"""营销活动：按单位类型匹配、专项优先、海报按客户轮询。"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Campaign, CampaignPoster, CampaignPosterSend, SystemConfig

AUDIENCE_GENERAL = "通用"
STATUS_ENABLED = "enabled"
STATUS_DISABLED = "disabled"
MAX_INJECT_CAMPAIGNS = 2
EMPTY_CUSTOMER_CAMPAIGN_BLOCK = "当前无针对该客户的进行中活动。禁止编造优惠或活动。"
EMPTY_STAFF_CAMPAIGN_BLOCK = "当前没有进行中的活动。"
DEFAULT_UNIT_TYPE_CHOICES = ["学校", "卫健委", "消防", "街道办", "银行", "税务", "其他"]
OTHER_UNIT_TYPE = "其他"
# 历史/别名 → 标准选项；「其他」桶排除时需一并视为具名类型
UNIT_TYPE_ALIASES = {"医院": "卫健委", "税务局": "税务"}
_FORBIDDEN_TAG_HINTS = ("禁止打扰", "勿打扰", "已删除")


def normalize_audience(raw) -> list[str]:
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def parse_unit_type_choices(raw: str | None) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return list(DEFAULT_UNIT_TYPE_CHOICES)
    return [x.strip() for x in text.split(",") if x.strip()]


def named_unit_types_for_other_bucket(
    choices: Sequence[str] | None = None,
) -> frozenset[str]:
    """具体单位性质（不含「其他」），含指向它们的别名，供群发「其他」排除。"""
    source = list(choices) if choices is not None else list(DEFAULT_UNIT_TYPE_CHOICES)
    named: set[str] = set()
    for item in source:
        name = str(item or "").strip()
        if not name or name == OTHER_UNIT_TYPE:
            continue
        named.add(name)
        named.add(UNIT_TYPE_ALIASES.get(name, name))
    for alias, canonical in UNIT_TYPE_ALIASES.items():
        if canonical in named:
            named.add(alias)
    return frozenset(named)


async def load_unit_type_choices(db: AsyncSession) -> list[str]:
    res = await db.execute(
        select(SystemConfig).where(SystemConfig.config_key == "unit_type_choices")
    )
    row = res.scalars().first()
    raw = (row.config_value or "").strip() if row else ""
    return parse_unit_type_choices(raw)


def campaign_is_generic(types: Sequence[str]) -> bool:
    return AUDIENCE_GENERAL in types


def campaign_matches_unit(types: Sequence[str], unit_type: str | None) -> bool:
    if campaign_is_generic(types):
        return True
    ut = (unit_type or "").strip()
    return bool(ut) and ut in types


def tags_forbid_outreach(tag_names: Iterable[str] | None) -> bool:
    for name in tag_names or []:
        text = str(name or "")
        if any(h in text for h in _FORBIDDEN_TAG_HINTS):
            return True
    return False


def select_campaigns_for_unit(
    campaigns: Sequence[Campaign],
    unit_type: str | None,
    *,
    forbidden_outreach: bool = False,
) -> list[Campaign]:
    """有专项（未勾通用）时只推专项；否则推通用。按 priority、开始时间降序，最多 2 场。"""
    if forbidden_outreach:
        return []
    matched: list[Campaign] = []
    for camp in campaigns:
        types = normalize_audience(camp.audience_unit_types)
        if campaign_matches_unit(types, unit_type):
            matched.append(camp)
    specific = [
        c for c in matched if not campaign_is_generic(normalize_audience(c.audience_unit_types))
    ]
    pool = specific if specific else matched
    pool.sort(
        key=lambda c: (int(c.priority or 0), c.start_at or datetime.min),
        reverse=True,
    )
    return pool[:MAX_INJECT_CAMPAIGNS]


def _fmt_dt(value: datetime | None) -> str:
    if not value:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M")


def format_campaign_block(campaigns: Sequence[Campaign]) -> str:
    if not campaigns:
        return EMPTY_CUSTOMER_CAMPAIGN_BLOCK
    lines = [
        "下列活动仅作可选话术依据，禁止编造规则里没有的折扣、赠品或截止日。",
        "何时可提：客户问活动/优惠；销售明确要求带活动；推品理由刚好契合门槛；开场白能顺口带半句。",
        "何时不要提：日常跟进、改资料、闲聊、答非活动问题。勿硬塞活动；一次最多点一个活动、一两句即可，不要复述全部规则。",
        "",
    ]
    for camp in campaigns:
        audience = "、".join(normalize_audience(camp.audience_unit_types)) or "—"
        rules = (camp.rules or "").strip() or "（未填写）"
        lines.append(f"- 名称：{camp.name}")
        lines.append(f"  时间：{_fmt_dt(camp.start_at)} ～ {_fmt_dt(camp.end_at)}")
        lines.append(f"  面向：{audience}")
        lines.append(f"  规则：{rules}")
    return "\n".join(lines)


def format_campaign_brief(campaigns: Sequence[Campaign]) -> str:
    if not campaigns:
        return ""
    parts: list[str] = []
    for camp in campaigns:
        rules = (camp.rules or "").strip().replace("\n", " ")
        if len(rules) > 80:
            rules = rules[:79] + "…"
        window = f"{_fmt_dt(camp.start_at)}～{_fmt_dt(camp.end_at)}"
        if rules:
            parts.append(f"{camp.name}（{window}；{rules}）")
        else:
            parts.append(f"{camp.name}（{window}）")
    return "；".join(parts)


def format_staff_campaign_block(campaigns: Sequence[Campaign]) -> str:
    if not campaigns:
        return EMPTY_STAFF_CAMPAIGN_BLOCK
    lines = [
        "下列为当前进行中的活动（内部一览）。同事未问活动时不要把回答写成促销稿。",
        "示范发给客户的话术时最多自然带一句，且须按该客户单位类型匹配。勿硬塞活动。",
        "",
    ]
    for camp in campaigns:
        types = normalize_audience(camp.audience_unit_types)
        kind = "通用" if campaign_is_generic(types) else "专项"
        audience = "、".join(types) or "—"
        rules = (camp.rules or "").strip() or "（未填写）"
        lines.append(f"- [{kind}] {camp.name}")
        lines.append(f"  时间：{_fmt_dt(camp.start_at)} ～ {_fmt_dt(camp.end_at)}")
        lines.append(f"  面向：{audience}")
        lines.append(f"  规则：{rules}")
    return "\n".join(lines)


def pick_next_poster(
    posters: Sequence[CampaignPoster],
    sends: Sequence[tuple[int, datetime]],
) -> CampaignPoster | None:
    """未对该客户发过的海报优先；都发过则从该客户最早发出的那张开始轮询。"""
    active = [p for p in posters if getattr(p, "is_active", True)]
    if not active:
        return None
    last_by_poster: dict[int, datetime] = {}
    for poster_id, sent_at in sends:
        if poster_id is None or sent_at is None:
            continue
        prev = last_by_poster.get(int(poster_id))
        if prev is None or sent_at > prev:
            last_by_poster[int(poster_id)] = sent_at
    unused = [p for p in active if int(p.id) not in last_by_poster]
    if unused:
        return min(
            unused,
            key=lambda p: (int(p.send_count or 0), int(p.sort_order or 0), int(p.id or 0)),
        )
    return min(
        active,
        key=lambda p: (
            last_by_poster.get(int(p.id), datetime.min),
            int(p.sort_order or 0),
            int(p.id or 0),
        ),
    )


async def audience_choices_from_config(db: AsyncSession) -> list[tuple[str, str]]:
    units = await load_unit_type_choices(db)
    labels = [AUDIENCE_GENERAL, *units]
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for name in labels:
        if name in seen:
            continue
        seen.add(name)
        out.append((name, name))
    return out


async def list_running_campaigns(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[Campaign]:
    ts = now or datetime.now()
    stmt = (
        select(Campaign)
        .where(Campaign.status == STATUS_ENABLED)
        .where(Campaign.start_at <= ts)
        .where(Campaign.end_at >= ts)
        .order_by(Campaign.priority.desc(), Campaign.start_at.desc())
    )
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def campaigns_for_customer(
    db: AsyncSession,
    *,
    unit_type: str | None,
    forbidden_outreach: bool = False,
    now: datetime | None = None,
) -> list[Campaign]:
    running = await list_running_campaigns(db, now=now)
    return select_campaigns_for_unit(
        running, unit_type, forbidden_outreach=forbidden_outreach
    )


async def assemble_campaign_block(
    db: AsyncSession,
    *,
    unit_type: str | None,
    forbidden_outreach: bool = False,
    now: datetime | None = None,
) -> str:
    camps = await campaigns_for_customer(
        db,
        unit_type=unit_type,
        forbidden_outreach=forbidden_outreach,
        now=now,
    )
    return format_campaign_block(camps)


async def assemble_staff_campaign_block(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> str:
    running = await list_running_campaigns(db, now=now)
    return format_staff_campaign_block(running)


async def attach_campaign_briefs(db: AsyncSession, payloads: list[dict]) -> None:
    """给任务分配/激活快照补 campaign_brief；无匹配则为空串。"""
    if not payloads:
        return
    running = await list_running_campaigns(db)
    for item in payloads:
        forbidden = tags_forbid_outreach(item.get("profile_tags") or [])
        matched = select_campaigns_for_unit(
            running,
            item.get("unit_type"),
            forbidden_outreach=forbidden,
        )
        item["campaign_brief"] = format_campaign_brief(matched)


async def pick_poster_for_customer(
    db: AsyncSession,
    *,
    campaign_id: int,
    raw_customer_id: str,
) -> CampaignPoster | None:
    cid = int(campaign_id)
    rid = (raw_customer_id or "").strip()
    if not rid:
        return None
    posters_res = await db.execute(
        select(CampaignPoster)
        .where(CampaignPoster.campaign_id == cid)
        .where(CampaignPoster.is_active.is_(True))
        .order_by(CampaignPoster.sort_order.asc(), CampaignPoster.id.asc())
    )
    posters = list(posters_res.scalars().all())
    if not posters:
        return None
    send_res = await db.execute(
        select(CampaignPosterSend.poster_id, CampaignPosterSend.sent_at).where(
            CampaignPosterSend.campaign_id == cid,
            CampaignPosterSend.raw_customer_id == rid,
        )
    )
    sends = [(int(pid), ts) for pid, ts in send_res.all() if pid is not None and ts is not None]
    return pick_next_poster(posters, sends)


async def record_poster_send(
    db: AsyncSession,
    *,
    poster: CampaignPoster,
    raw_customer_id: str,
    sales_wechat_id: str | None = None,
    actor_user_id: int | None = None,
    outbound_action_id: int | None = None,
    sent_at: datetime | None = None,
) -> CampaignPosterSend:
    row = CampaignPosterSend(
        campaign_id=int(poster.campaign_id),
        poster_id=int(poster.id),
        raw_customer_id=(raw_customer_id or "").strip(),
        sales_wechat_id=(sales_wechat_id or "").strip() or None,
        actor_user_id=actor_user_id,
        outbound_action_id=outbound_action_id,
        sent_at=sent_at or datetime.now(),
    )
    db.add(row)
    poster.send_count = int(poster.send_count or 0) + 1
    await db.flush()
    return row
