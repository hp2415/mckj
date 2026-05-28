"""
提示词预算估算与分批切分；失败时缩小 batch 的重试参数。
"""
from __future__ import annotations

import json
import os
from typing import Any

PROMPT_CHAR_BUDGET = int(os.getenv("TASK_ALLOCATION_PROMPT_CHAR_BUDGET") or "120000")
DEFAULT_LLM_BATCH_SIZE = int(os.getenv("TASK_ALLOCATION_LLM_BATCH_SIZE") or "30")
MIN_LLM_BATCH_SIZE = int(os.getenv("TASK_ALLOCATION_LLM_BATCH_MIN") or "8")


def estimate_messages_chars(messages: list[dict[str, str]]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)


def estimate_feature_batch_chars(features: list[dict[str, Any]]) -> int:
    return len(json.dumps(features, ensure_ascii=False, separators=(",", ":")))


def split_feature_batches(
    features: list[dict[str, Any]],
    *,
    batch_size: int | None = None,
    char_budget: int | None = None,
    system_user_overhead: int = 8000,
) -> list[list[dict[str, Any]]]:
    """
    将特征列表切成多批，使每批 customers JSON + 固定开销不超过 char_budget。
    """
    if not features:
        return []
    bs = max(MIN_LLM_BATCH_SIZE, int(batch_size or DEFAULT_LLM_BATCH_SIZE))
    budget = int(char_budget or PROMPT_CHAR_BUDGET)
    batches: list[list[dict[str, Any]]] = []
    i = 0
    while i < len(features):
        chunk: list[dict[str, Any]] = []
        while i < len(features):
            trial = chunk + [features[i]]
            if (
                len(trial) > bs
                or estimate_feature_batch_chars(trial) + system_user_overhead > budget
            ):
                break
            chunk = trial
            i += 1
        if not chunk:
            chunk = [features[i]]
            i += 1
        batches.append(chunk)
    return batches


def batch_task_cap(total_cap: int, batch_index: int, batch_count: int) -> int:
    """将总 cap 均分到各批，最后一批吃余数。"""
    if batch_count <= 0:
        return total_cap
    base = max(1, total_cap // batch_count)
    rem = total_cap % batch_count
    return base + (1 if batch_index < rem else 0)


def shrink_batch_params(batch_size: int, attempt: int) -> int:
    """重试时缩小 batch。"""
    factor = max(1, 2**attempt)
    return max(MIN_LLM_BATCH_SIZE, batch_size // factor)
