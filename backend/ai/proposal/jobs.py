from __future__ import annotations

import asyncio
import os
import socket
from datetime import datetime, timedelta

from sqlalchemy import desc, select, update

from ai.context import ContextAssembler
from ai.proposal.composer import compose_spec
from ai.proposal.extractor import (
    MAX_FEEDBACK_HISTORY,
    parse_discount_rate,
    sanitize_constraints,
)
from ai.proposal.renderer import render_xlsx
from ai.proposal.service import downloads_root, render_preview_text
from ai.proposal.understand import understand_constraints
from ai.proposal.policy import get_proposal_policy
from core.logger import logger
from database import AsyncSessionLocal
from models import AiProposal, AiProposalVersion, ChatMessage


POLL_SECONDS = 1.5


def _worker_id() -> str:
    return (os.getenv("PROPOSAL_WORKER_ID") or socket.gethostname() or "proposal-worker")[:80]


async def _claim_next() -> int | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AiProposal)
            .where(AiProposal.status == "queued")
            .order_by(AiProposal.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        proposal = result.scalars().first()
        if proposal is None:
            return None
        proposal.status = "running"
        proposal.locked_by = _worker_id()
        proposal.locked_at = datetime.now()
        proposal.error_message = None
        await db.commit()
        return proposal.id


async def _recover_stale_jobs() -> None:
    cutoff = datetime.now() - timedelta(minutes=20)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(AiProposal)
            .where(AiProposal.status == "running")
            .where(AiProposal.locked_at < cutoff)
            .values(status="queued", locked_by=None, locked_at=None)
        )
        await db.commit()


async def _context_summary(db, proposal: AiProposal) -> str:
    if not proposal.raw_customer_id:
        return "自由对话，无客户上下文；仅按用户要求选品。"
    ctx = await ContextAssembler(db).assemble(
        proposal.user_id,
        raw_customer_id=proposal.raw_customer_id,
        resolved_sales_wechat_id=proposal.sales_wechat_id,
    )
    return "\n".join(
        str(ctx.get(key) or "")
        for key in ("customer_card", "ai_profile", "budget_amount", "chat_summary", "order_summary")
    )


async def _latest_version(db, proposal_id: int) -> AiProposalVersion | None:
    result = await db.execute(
        select(AiProposalVersion)
        .where(AiProposalVersion.proposal_id == proposal_id)
        .order_by(desc(AiProposalVersion.version))
        .limit(1)
    )
    return result.scalars().first()


async def _process(proposal_id: int) -> None:
    async with AsyncSessionLocal() as db:
        proposal = await db.get(AiProposal, proposal_id)
        if proposal is None:
            return
        previous = await _latest_version(db, proposal.id)
        constraints = sanitize_constraints(proposal.constraint_json)
        source = str(constraints.get("source") or ("revise" if previous else "generate"))
        context_summary = await _context_summary(db, proposal)
        prior_lines: list[dict] = []

        if previous is not None and source == "revise":
            prior = sanitize_constraints((previous.spec_json or {}).get("meta") or {})
            prior.update({key: value for key, value in constraints.items() if value is not None})
            prior_lines = [
                {
                    "product_id": line.get("product_db_id"),
                    "name": line.get("product_name"),
                    "platform_price": line.get("platform_price"),
                    "qty_per_person": line.get("qty_per_person") or 1,
                }
                for line in (previous.spec_json or {}).get("lines") or []
            ]
            # 修订反馈交给需求理解小模型；带上 prior_lines，避免把「每人3件」读成预算。
            parsed = await understand_constraints(
                db,
                proposal.query,
                prior=prior,
                prior_lines=prior_lines,
                user_id=proposal.user_id,
            )
            for key in (
                "per_capita_budget",
                "headcount",
                "include_keywords",
                "exclude_keywords",
                "item_kinds",
            ):
                if parsed.get(key) is not None:
                    prior[key] = parsed[key]
            # 店铺可被清空（「不局限行唐」）；空列表也要写回去，否则会一直锁在旧店
            if "shop_keywords" in parsed:
                prior["shop_keywords"] = list(parsed.get("shop_keywords") or [])
            rate, rate_source = parse_discount_rate(
                proposal.query,
                default=(await get_proposal_policy()).default_discount_rate,
            )
            if rate_source == "dialog":
                prior["discount_rate"] = rate
                prior["discount_source"] = rate_source
            elif parsed.get("discount_source") == "dialog":
                prior["discount_rate"] = parsed["discount_rate"]
                prior["discount_source"] = "dialog"
            history = list(prior.get("feedback_history") or [])
            history.append(proposal.query)
            prior["feedback_history"] = history[-MAX_FEEDBACK_HISTORY:]
            constraints = sanitize_constraints(prior)

        spec = await compose_spec(
            db,
            constraints=constraints,
            context_summary=context_summary,
            user_id=proposal.user_id,
            customer_id=proposal.raw_customer_id,
            prior_lines=prior_lines,
        )
        next_version = int(proposal.current_version or 0) + 1
        relative_path = f"proposals/{proposal.id}/v{next_version}.xlsx"
        target = downloads_root() / relative_path
        await render_xlsx(spec, target)

        version = AiProposalVersion(
            proposal_id=proposal.id,
            version=next_version,
            spec_json=spec,
            file_path=relative_path.replace("\\", "/"),
            source=source,
            user_feedback=proposal.query if source == "revise" else None,
        )
        db.add(version)
        proposal.current_version = next_version
        proposal.status = "ready"
        proposal.constraint_json = sanitize_constraints(spec.get("meta") or constraints)
        proposal.locked_by = None
        proposal.locked_at = None

        if proposal.chat_message_id:
            message = await db.get(ChatMessage, proposal.chat_message_id)
            if message is not None:
                message.content = render_preview_text(proposal, spec, next_version)

        await db.commit()


async def _mark_failed(proposal_id: int, error: Exception) -> None:
    async with AsyncSessionLocal() as db:
        proposal = await db.get(AiProposal, proposal_id)
        if proposal is None:
            return
        proposal.status = "failed"
        proposal.error_message = str(error)[:2000]
        proposal.locked_by = None
        proposal.locked_at = None
        await db.commit()


async def run_worker_loop() -> None:
    await _recover_stale_jobs()
    logger.info("proposal worker started id={}", _worker_id())
    while True:
        proposal_id = None
        try:
            proposal_id = await _claim_next()
            if proposal_id is None:
                await asyncio.sleep(POLL_SECONDS)
                continue
            await _process(proposal_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("proposal worker job failed id={}: {}", proposal_id, error)
            if proposal_id is not None:
                await _mark_failed(proposal_id, error)
            await asyncio.sleep(POLL_SECONDS)

