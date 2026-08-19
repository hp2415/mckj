from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import desc, select

from ai.proposal.extractor import (
    apply_constraint_defaults,
    missing_required_constraints,
    sanitize_constraints,
)
from ai.proposal.policy import FALLBACK_POLICY
from ai.proposal.understand import understand_constraints
from models import AiProposal, AiProposalVersion


def downloads_root() -> Path:
    backend_dir = Path(__file__).resolve().parents[2]
    return Path(os.getenv("DOWNLOADS_DIR") or backend_dir / "downloads")


def proposals_root() -> Path:
    """方案文件目录（不对外静态挂载）。相对路径仍为 proposals/{id}/vN.xlsx。"""
    configured = (os.getenv("PROPOSALS_DIR") or "").strip()
    if configured:
        return Path(configured)
    backend_dir = Path(__file__).resolve().parents[2]
    return backend_dir / "private"


def resolve_proposal_file(relative_path: str) -> Path | None:
    rel = (relative_path or "").replace("\\", "/").lstrip("/")
    if not rel or any(part == ".." for part in rel.split("/")):
        return None
    for root in (proposals_root(), downloads_root()):
        base = root.resolve()
        path = (root / rel).resolve()
        try:
            path.relative_to(base)
        except ValueError:
            continue
        if path.is_file():
            return path
    return None


async def find_latest_ready_proposal(
    db,
    *,
    user_id: int,
    raw_customer_id: str | None = None,
    sales_wechat_id: str | None = None,
) -> AiProposal | None:
    """找当前会话上下文下最近一份可修订的方案。

    ready 与 failed 都算：修订失败后仍保留上一版明细，用户继续反馈应落在同一份方案上，
    而不是悄悄跳到更早的另一份 ready 方案。
    """
    stmt = (
        select(AiProposal)
        .where(AiProposal.user_id == user_id)
        .where(AiProposal.status.in_(("ready", "failed")))
        .where(AiProposal.current_version > 0)
        .order_by(desc(AiProposal.id))
        .limit(1)
    )
    raw_id = (raw_customer_id or "").strip() or None
    if raw_id is None:
        stmt = stmt.where(AiProposal.raw_customer_id.is_(None))
    else:
        stmt = stmt.where(AiProposal.raw_customer_id == raw_id)
    sw = (sales_wechat_id or "").strip() or None
    if sw:
        stmt = stmt.where(
            (AiProposal.sales_wechat_id == sw) | (AiProposal.sales_wechat_id.is_(None))
        )
    return (await db.execute(stmt)).scalars().first()


async def enqueue_proposal(
    db,
    *,
    user_id: int,
    query: str,
    raw_customer_id: str | None = None,
    sales_wechat_id: str | None = None,
    chat_model: str | None = None,
) -> AiProposal:
    # 桌面端支持多模型并发；同一句会并行进入多个 gateway，方案只应入队一次。
    # 去重放在需求解析之前，省掉重复的小模型调用。
    cutoff = datetime.now() - timedelta(seconds=30)
    duplicate_stmt = (
        select(AiProposal)
        .where(AiProposal.user_id == user_id)
        .where(AiProposal.query == (query or "").strip())
        .where(AiProposal.created_at >= cutoff)
        .where(AiProposal.status.in_(["queued", "running", "ready"]))
        .order_by(desc(AiProposal.id))
        .limit(1)
    )
    raw_id = (raw_customer_id or "").strip() or None
    if raw_id is None:
        duplicate_stmt = duplicate_stmt.where(AiProposal.raw_customer_id.is_(None))
    else:
        duplicate_stmt = duplicate_stmt.where(AiProposal.raw_customer_id == raw_id)
    duplicate = (await db.execute(duplicate_stmt)).scalars().first()
    if duplicate is not None:
        return duplicate

    # 不继承上一份方案的人均/人数：真缺就让上层回落到「调整上一版」，
    # 那条路径还能带上一版明细，比重新起一份更连贯。
    constraints = await understand_constraints(db, query, user_id=user_id)
    constraints = apply_constraint_defaults(constraints)
    if (chat_model or "").strip():
        constraints["chat_model"] = chat_model.strip()
    missing = missing_required_constraints(constraints)
    if missing:
        raise ValueError("还需要补充：" + "、".join(missing))

    proposal = AiProposal(
        user_id=user_id,
        raw_customer_id=raw_id,
        sales_wechat_id=(sales_wechat_id or "").strip() or None,
        status="queued",
        query=(query or "").strip(),
        constraint_json=constraints,
    )
    db.add(proposal)
    await db.commit()
    await db.refresh(proposal)
    return proposal


async def enqueue_revision(
    db,
    *,
    proposal: AiProposal,
    feedback: str,
) -> AiProposal:
    text = (feedback or "").strip()
    if not text:
        raise ValueError("调整要求不能为空")
    if proposal.status in ("queued", "running"):
        raise ValueError("方案仍在生成中，请完成后再调整")
    proposal.query = text
    proposal.status = "queued"
    proposal.error_message = None
    constraints = sanitize_constraints(proposal.constraint_json)
    constraints["source"] = "revise"
    proposal.constraint_json = constraints
    await db.commit()
    await db.refresh(proposal)
    return proposal


async def latest_version(db, proposal_id: int) -> AiProposalVersion | None:
    result = await db.execute(
        select(AiProposalVersion)
        .where(AiProposalVersion.proposal_id == proposal_id)
        .order_by(desc(AiProposalVersion.version))
        .limit(1)
    )
    return result.scalars().first()


def render_preview_text(proposal: AiProposal, spec: dict, version: int) -> str:
    """服务端渲染的方案预览，用于回写对话气泡，切换会话后仍可见。"""
    totals = spec.get("totals") or {}
    meta = spec.get("meta") or {}
    lines = []
    for line in spec.get("lines") or []:
        per_person = int(line.get("qty_per_person") or 1)
        extra = f"（每人 {per_person} 件）" if per_person > 1 else ""
        cost = line.get("cost_price")
        if cost is not None:
            cost_text = f"，成本价 ¥{float(cost):.2f}"
        elif str(line.get("priced_by") or "") == "fallback_discount":
            zhe = float(meta.get("fallback_discount_rate") or FALLBACK_POLICY.fallback_discount_rate) * 10
            cost_text = f"，无成本价（按 {zhe:g} 折）"
        else:
            cost_text = ""
        lines.append(
            f"- {line.get('product_name') or ''} × {line.get('qty') or 0}{extra}，"
            f"优惠单价 ¥{float(line.get('promo_unit_price') or 0):.2f}{cost_text}"
        )
    budget = float(meta.get("per_capita_budget") or 0)
    budget_text = f"（人均预算 ¥{budget:.2f}）" if budget > 0 else ""
    has_uncosted = any(
        str(line.get("priced_by") or "") == "fallback_discount" or line.get("cost_price") is None
        for line in spec.get("lines") or []
    )
    if str(meta.get("discount_source") or "") == "dialog" and meta.get("discount_rate") is not None:
        pricing_note = f"\n折扣：{float(meta['discount_rate']) * 10:g} 折"
    elif not has_uncosted:
        margin = float(meta.get("gross_margin") or FALLBACK_POLICY.default_gross_margin)
        pricing_note = f"\n毛利率：{margin * 100:g}%"
    else:
        pricing_note = ""
    cost_total = totals.get("cost_total")
    cost_total_text = (
        f"\n成本合计：¥{float(cost_total):.2f}"
        if (not has_uncosted) and cost_total is not None
        else ""
    )
    return (
        f"方案 #{proposal.id} v{version} 已生成。\n\n### 方案预览\n"
        + "\n".join(lines)
        + f"\n\n人均优惠价：¥{float(totals.get('per_capita_promo') or 0):.2f}{budget_text}"
        + f"\n优惠总价：¥{float(totals.get('promo_total') or 0):.2f}"
        + cost_total_text
        + pricing_note
        + "\n\n如需调整，可直接回复“把……换成……”或“把毛利率改为25%”。"
    )


def proposal_payload(proposal: AiProposal, version: AiProposalVersion | None = None) -> dict:
    payload = {
        "id": proposal.id,
        "status": proposal.status,
        "current_version": proposal.current_version,
        "raw_customer_id": proposal.raw_customer_id,
        "sales_wechat_id": proposal.sales_wechat_id,
        "constraints": proposal.constraint_json or {},
        "error_message": proposal.error_message,
        "created_at": proposal.created_at.isoformat() if proposal.created_at else None,
        "updated_at": proposal.updated_at.isoformat() if proposal.updated_at else None,
    }
    if version is not None:
        payload["spec"] = version.spec_json
        payload["download_url"] = (
            f"/api/proposals/{proposal.id}/download?version={version.version}"
        )
    return payload

