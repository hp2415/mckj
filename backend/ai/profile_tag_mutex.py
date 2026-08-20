"""画像动态标签互斥消解。

提示词只能降低互斥组同时命中的概率；落库前必须按组只留一个。
增量画像里的已有标签仅作参考，以本次 LLM 输出为准；仅保留模型禁止改写的运营标签。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# 系统运营标签：画像模型禁止新打，已有则保留（不随漏选冲掉）
MANUAL_IMPORT_TAG_NAME = "📌 手动导入跟进"

# 联系频率档：20/30/40 及「老」前缀、可选「天」后缀
_FREQ_NAME_RE = re.compile(r"^老?(20|30|40)天?$")

_LIFECYCLE_NAMES = frozenset({"新客户", "老客户"})
_GENDER_NAMES = frozenset({"男", "女", "男性", "女性"})

# 用于目录文案：组 key → 标题
EXCLUSIVE_GROUP_TITLES: tuple[tuple[str, str], ...] = (
    ("frequency", "联系频率档（20/30/40/老20/老30/老40）"),
    ("lifecycle", "新老客户"),
    ("gender", "性别"),
)


def exclusive_group_key(name: str | None) -> str | None:
    n = (name or "").strip()
    if not n:
        return None
    if n in _LIFECYCLE_NAMES:
        return "lifecycle"
    if n in _GENDER_NAMES:
        return "gender"
    if _FREQ_NAME_RE.fullmatch(n):
        return "frequency"
    return None


def _iter_tag_rows(rows: Iterable[Any]) -> Iterable[tuple[int, str]]:
    for r in rows or []:
        if isinstance(r, Mapping):
            tid, name = r.get("id"), r.get("name")
        else:
            tid, name = getattr(r, "id", None), getattr(r, "name", None)
        try:
            tid_i = int(tid)
        except (TypeError, ValueError):
            continue
        yield tid_i, str(name or "").strip()


def format_exclusive_groups_catalog(rows: Iterable[Any]) -> str:
    """把互斥组写成带真实 id 的提示词片段；某组在目录中一个都没有则省略。"""
    by_group: dict[str, list[tuple[int, str]]] = {k: [] for k, _ in EXCLUSIVE_GROUP_TITLES}
    seen: set[int] = set()
    for tid, name in _iter_tag_rows(rows):
        if tid in seen:
            continue
        key = exclusive_group_key(name)
        if not key:
            continue
        seen.add(tid)
        by_group[key].append((tid, name))
    lines: list[str] = []
    for key, title in EXCLUSIVE_GROUP_TITLES:
        items = by_group.get(key) or []
        if not items:
            continue
        parts = [f"id={tid}「{name}」" for tid, name in items]
        lines.append(f"- {title}：{'、'.join(parts)}（最多 1 个）")
    if not lines:
        return ""
    return (
        "【互斥组（每组最多 1 个 id；证据不足则该组不打）】\n"
        + "\n".join(lines)
        + "\n系统落库时若同组出现多个，只保留本次输出中该组最后一个 id。"
    )


def apply_exclusive_profile_tags(
    ids: list[int],
    id_to_name: Mapping[int, str] | None,
) -> list[int]:
    """每个互斥组只保留 ids 中最后一次出现的成员；非互斥标签去重保序。"""
    names = id_to_name or {}
    last_in_group: dict[str, int] = {}
    for tid in ids:
        key = exclusive_group_key((names.get(tid) or "").strip())
        if key:
            last_in_group[key] = tid
    keep_mutex = set(last_in_group.values())
    seen: set[int] = set()
    out: list[int] = []
    for tid in ids:
        if tid in seen:
            continue
        key = exclusive_group_key((names.get(tid) or "").strip())
        if key is not None and tid not in keep_mutex:
            continue
        seen.add(tid)
        out.append(tid)
    return out


def finalize_matched_profile_tag_ids(
    existing: list[dict] | None,
    llm_raw: Any,
    *,
    id_to_name: Mapping[int, str] | None = None,
) -> list[int]:
    """
    本次画像标签集合：以 LLM 输出为准（旧标签仅作分析参考，不并集强制保留）。
    例外：已打上的「📌 手动导入跟进」保留；模型新打上该标签则丢弃。
    最后按互斥组消解。
    """
    from crud import parse_profile_tag_ids

    names: dict[int, str] = dict(id_to_name or {})
    preserve_manual: list[int] = []
    for t in existing or []:
        try:
            tid = int(t.get("id"))
        except (TypeError, ValueError):
            continue
        name = str(t.get("name") or "").strip()
        if name:
            names[tid] = name
        if name == MANUAL_IMPORT_TAG_NAME and tid not in preserve_manual:
            preserve_manual.append(tid)

    seen: set[int] = set()
    llm_ids: list[int] = []
    for tid in parse_profile_tag_ids(llm_raw):
        if tid in seen:
            continue
        name = (names.get(tid) or "").strip()
        if name == MANUAL_IMPORT_TAG_NAME and tid not in preserve_manual:
            continue
        seen.add(tid)
        llm_ids.append(tid)

    for tid in preserve_manual:
        if tid not in seen:
            llm_ids.append(tid)
            seen.add(tid)

    return apply_exclusive_profile_tags(llm_ids, names)
