"""
任务分配候选特征快照：gzip 落库，支撑离线回放（P0）。
"""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from core.logger import logger
from models import TaskAllocationInputSnapshot


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, set):
        return list(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def should_persist_input_snapshot(
    limits: dict[str, Any] | None,
    *,
    sales_wechat_id: str,
    ref_date: date,
) -> bool:
    lim = limits or {}
    if not bool(lim.get("input_snapshot_enabled", True)):
        return False
    try:
        ratio = float(lim.get("input_snapshot_sample_ratio", 1.0))
    except (TypeError, ValueError):
        ratio = 1.0
    ratio = max(0.0, min(1.0, ratio))
    if ratio >= 1.0:
        return True
    if ratio <= 0.0:
        return False
    key = f"{(sales_wechat_id or '').strip()}|{ref_date.isoformat()}"
    bucket = int(hashlib.md5(key.encode("utf-8")).hexdigest()[:8], 16) % 10000
    return bucket < int(ratio * 10000)


def compress_payloads(payloads: list[dict[str, Any]]) -> tuple[bytes, int]:
    raw = json.dumps(payloads or [], ensure_ascii=False, default=_json_default).encode("utf-8")
    return gzip.compress(raw, compresslevel=6), len(payloads or [])


def decompress_payloads(blob: bytes) -> list[dict[str, Any]]:
    data = json.loads(gzip.decompress(blob).decode("utf-8"))
    return data if isinstance(data, list) else []


async def persist_allocation_input_snapshot(
    db,
    *,
    batch_id: int,
    sales_wechat_id: str,
    ref_date: date,
    payloads: list[dict[str, Any]],
    prompt_version_id: int | None,
    limits: dict[str, Any] | None = None,
) -> TaskAllocationInputSnapshot | None:
    """按开关/抽样写入候选特征快照；失败只记日志，不影响分配主流程。"""
    sw = (sales_wechat_id or "").strip()
    if not batch_id or not sw:
        return None
    if not should_persist_input_snapshot(limits, sales_wechat_id=sw, ref_date=ref_date):
        return None
    try:
        blob, count = compress_payloads(payloads)
        # MySQL MEDIUMBLOB = 16MB-1；超限时跳过快照，避免拖垮整批分配。
        if len(blob) > 16_777_215:
            logger.warning(
                "任务分配输入快照过大已跳过 batch_id={} sw={} gzip_bytes={}",
                batch_id,
                sw,
                len(blob),
            )
            return None
        async with db.begin_nested():
            row = TaskAllocationInputSnapshot(
                batch_id=int(batch_id),
                sales_wechat_id=sw,
                ref_date=ref_date,
                payload_gzip=blob,
                payload_count=int(count),
                prompt_version_id=int(prompt_version_id) if prompt_version_id is not None else None,
            )
            db.add(row)
            await db.flush()
        return row
    except Exception as e:
        logger.warning(
            "任务分配输入快照写入失败 batch_id={} sw={}: {}",
            batch_id,
            sw,
            e,
        )
        return None


def extract_prompt_version_id(meta: dict[str, Any] | None, *, icebreaker: bool = False) -> int | None:
    """从 llm_meta / ice_snap / scalable pipe_meta 中取出 prompt_version_id。"""
    m = meta if isinstance(meta, dict) else {}
    if icebreaker:
        ice = m.get("icebreaker") if isinstance(m.get("icebreaker"), dict) else m
        candidates = [
            ice.get("prompt_version_id") if isinstance(ice, dict) else None,
            m.get("prompt_version_id"),
        ]
    else:
        pipe = m.get("scalable_pipeline") if isinstance(m.get("scalable_pipeline"), dict) else {}
        batch_metas = pipe.get("llm_batch_meta") if isinstance(pipe, dict) else None
        from_batch = None
        if isinstance(batch_metas, list):
            for bm in batch_metas:
                if isinstance(bm, dict) and bm.get("prompt_version_id") is not None:
                    from_batch = bm.get("prompt_version_id")
                    break
        candidates = [
            m.get("prompt_version_id"),
            pipe.get("prompt_version_id") if isinstance(pipe, dict) else None,
            from_batch,
        ]
    for v in candidates:
        if v is None or v == "":
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None
