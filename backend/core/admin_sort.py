"""管理后台列表：从 column_list 自动推断可排序列与默认排序。"""
from __future__ import annotations

from typing import Any, List, Sequence, Tuple, Union, no_type_check

from sqlalchemy import inspect
from sqlalchemy.orm import InstrumentedAttribute
from sqladmin.models import ModelView, ModelViewMeta

# 列表中的虚拟列（无对应 SQL 列，不可排序）
_VIRTUAL_COLUMNS = frozenset(
    {
        "quick_action",
        "profile_status",
        "router_status",
        "published_version",
        "template_preview",
        "relations_links",
        "chat_links",
        "sales_wechat_bindings_count",
        "retry_action",
        "content_len",
    }
)

# 默认排序优先级（字段名, 是否降序）
_DEFAULT_SORT_PRIORITY: List[Tuple[str, bool]] = [
    ("created_at", True),
    ("updated_at", True),
    ("published_at", True),
    ("transfer_time", True),
    ("last_chat_time", True),
    ("synced_at", True),
    ("due_date", False),
    ("priority_rank", False),
    ("period_start", True),
    ("id", True),
]


def _column_key(col: Any) -> str:
    if isinstance(col, str):
        return col
    if isinstance(col, InstrumentedAttribute):
        return col.key
    return getattr(col, "key", str(col))


def _dotted_path_is_sortable(model: type, path: str) -> bool:
    parts = path.split(".")
    current = model
    for i, part in enumerate(parts):
        mapper = inspect(current)
        if part in mapper.columns:
            return i == len(parts) - 1
        rel = mapper.relationships.get(part)
        if rel is not None:
            current = rel.mapper.class_
            continue
        return False
    return False


def infer_sortable_columns(
    model: type, column_list: Sequence[Any]
) -> List[Any]:
    """从 column_list 提取可在 SQLAdmin 表头点击排序的列。"""
    sortable: List[Any] = []
    seen: set[str] = set()
    for col in column_list or []:
        key = _column_key(col)
        if not key or key in _VIRTUAL_COLUMNS or key in seen:
            continue
        if "." in key:
            if _dotted_path_is_sortable(model, key):
                sortable.append(col)
                seen.add(key)
            continue
        mapper = inspect(model)
        if key in mapper.columns:
            sortable.append(col)
            seen.add(key)
    return sortable


def infer_default_sort(
    sortable_list: Sequence[Any],
) -> Union[List[Tuple[Any, bool]], Tuple[Any, bool], Any]:
    """为列表页选择合理的默认排序（优先时间倒序）。"""
    if not sortable_list:
        return []
    keys = [_column_key(c) for c in sortable_list]
    for field_name, is_desc in _DEFAULT_SORT_PRIORITY:
        for i, key in enumerate(keys):
            if key == field_name or key.endswith(f".{field_name}"):
                return [(sortable_list[i], is_desc)]
    return [(sortable_list[0], True)]


class AdminModelViewMeta(ModelViewMeta):
    """在 ModelView 注册时自动补全排序配置（sqladmin 不走 __init_subclass__）。"""

    @no_type_check
    def __new__(mcs, name, bases, attrs: dict, **kwargs: Any):
        cls = super().__new__(mcs, name, bases, attrs, **kwargs)
        model = kwargs.get("model")
        if not model:
            return cls
        if "column_sortable_list" not in attrs:
            col_list = attrs.get("column_list") or []
            cls.column_sortable_list = infer_sortable_columns(model, col_list)
        if "column_default_sort" not in attrs:
            if "column_sortable_list" in attrs:
                sortable = attrs["column_sortable_list"]
            else:
                sortable = getattr(cls, "column_sortable_list", [])
            cls.column_default_sort = infer_default_sort(sortable)
        return cls


class AdminModelView(ModelView, metaclass=AdminModelViewMeta):
    """子类未显式配置 column_sortable_list / column_default_sort 时自动推断。"""

    pass
