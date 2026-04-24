"""
提示词场景化运行时所用的纯数据结构。

这些 dataclass 与 ORM（models.py 中的 PromptScenario/PromptVersion/...）解耦，
Store 层从 DB 读出后转换成这些结构，Renderer/Service 层只消费它们；
便于测试、缓存与未来规则引擎覆盖参数。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---- 模板与文档引用 ----

@dataclass
class PromptTemplate:
    """system prompt 模板本体。

    system 文本中可用 {{var}} 占位，渲染时由 ctx 填充。
    变量缺失时走 renderer 的兜底（"未知"/"暂无"）。
    user: 可选的第二段模板（如客户画像：system=JSON 约束，user=任务与上下文），
    同样支持 {{var}}，渲染后作为 user 消息；未配置时由调用方自行兜底。
    """
    system: str
    user: Optional[str] = None
    notes: Optional[str] = None


@dataclass
class DocInjectSpec:
    """单条文档注入规则。"""
    doc_key: str
    title: str = ""
    required: bool = False
    max_chars: Optional[int] = None
    # None 表示取该 doc 的当前 published 版本；否则指定特定版本 id
    doc_version_id: Optional[int] = None


@dataclass
class PromptParams:
    """对 LLM 调用参数的可选覆盖。"""
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    model: Optional[str] = None
    tools_enabled: Optional[bool] = None


@dataclass
class PromptVersionView:
    """PromptVersion 的运行时视图（非 ORM）。"""
    id: int
    scenario_key: str
    scenario_name: str
    scenario_tools_enabled: bool
    version: int
    status: str
    template: PromptTemplate
    doc_refs: list[DocInjectSpec] = field(default_factory=list)
    params: PromptParams = field(default_factory=PromptParams)
    notes: Optional[str] = None


# ---- 服务返回 ----

@dataclass
class PromptResolution:
    """PromptService.resolve 的返回结构。

    messages: 已经包含 system/history/user 的完整 OpenAI messages 列表。
    tools_enabled: 是否允许本次请求使用 function calling 工具。
    params: 对 LLM 调用参数的可选覆盖（None 表示沿用 gateway 默认）。
    meta: 审计/排障辅助信息，至少包含命中 scenario/version/doc_versions 及规则链路。
    """
    messages: list[dict]
    tools_enabled: bool = True
    params: PromptParams = field(default_factory=PromptParams)
    meta: dict[str, Any] = field(default_factory=dict)


# ---- 反序列化辅助 ----

def template_from_json(data: Any) -> PromptTemplate:
    if not isinstance(data, dict):
        return PromptTemplate(system=str(data or ""))
    u = data.get("user")
    u_str = str(u).strip() if u is not None else ""
    return PromptTemplate(
        system=str(data.get("system") or ""),
        user=u_str if u_str else None,
        notes=data.get("notes"),
    )


def doc_refs_from_json(data: Any) -> list[DocInjectSpec]:
    if not isinstance(data, list):
        return []
    out: list[DocInjectSpec] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        key = item.get("doc_key")
        if not key:
            continue
        max_chars_raw = item.get("max_chars")
        try:
            max_chars = int(max_chars_raw) if max_chars_raw not in (None, "", 0) else None
        except (TypeError, ValueError):
            max_chars = None
        doc_ver_raw = item.get("doc_version_id")
        try:
            doc_version_id = int(doc_ver_raw) if doc_ver_raw not in (None, "") else None
        except (TypeError, ValueError):
            doc_version_id = None
        out.append(
            DocInjectSpec(
                doc_key=str(key),
                title=str(item.get("title") or ""),
                required=bool(item.get("required", False)),
                max_chars=max_chars,
                doc_version_id=doc_version_id,
            )
        )
    return out


def params_from_json(data: Any) -> PromptParams:
    if not isinstance(data, dict):
        return PromptParams()
    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    def _i(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    tools_val = data.get("tools_enabled")
    tools_bool: Optional[bool]
    if tools_val is None:
        tools_bool = None
    else:
        tools_bool = bool(tools_val)
    return PromptParams(
        temperature=_f(data.get("temperature")),
        max_tokens=_i(data.get("max_tokens")),
        model=(str(data["model"]) if data.get("model") else None),
        tools_enabled=tools_bool,
    )


def template_to_dict(t: PromptTemplate) -> dict:
    d = asdict(t)
    if not d.get("user"):
        d.pop("user", None)
    if not d.get("notes"):
        d.pop("notes", None)
    return d
