"""
PromptRenderer：把 PromptTemplate + ctx + docs 渲染为最终 system 文本，并拼成 messages。

设计要点：
- 模板占位使用 {{var}} 形式（双大括号），避免与自然文本中的花括号冲突。
- ctx 中常用键：customer_card / ai_profile / order_summary / chat_summary /
  budget_amount / purchase_type / ai_history 等（见 ContextAssembler.assemble）。
  缺失时走 DEFAULT_FALLBACKS 兜底（参考旧 prompts.py 的行为：未知 / 暂无）。
- {{current_date}} 为内置变量，始终注入"今天的中文日期"。
- doc 注入块：按 DocInjectSpec 的顺序拼在 system 末尾，标题前会加 "## "。
- max_chars: 只做"尾部省略"截断，避免复杂摘要；超长 doc 只保留前 max_chars 字符 + "…（已截断）"。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

from ai.prompt_models import PromptTemplate, DocInjectSpec


# 与旧 prompts.py 行为一致的兜底
DEFAULT_FALLBACKS: dict[str, str] = {
    "customer_card": "未知",
    "ai_profile": "暂无",
    "order_summary": "暂无",
    "chat_summary": "暂无",
    "ai_history": "暂无",
    "budget_amount": "未知",
    "purchase_type": "未知",
    "basic_info": "暂无",
    "chat_context": "暂无",
    "order_context": "暂无",
}

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _builtin_vars() -> dict[str, str]:
    return {
        "current_date": datetime.now().strftime("%Y年%m月%d日"),
    }


def _truncate(text: str, max_chars: int | None) -> str:
    if not text:
        return ""
    if not max_chars or max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…（已截断）"


def _substitute(tpl: str, values: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name in values and values[name] is not None:
            return str(values[name])
        # 兜底：常用键走 DEFAULT_FALLBACKS；其它保持空串
        return DEFAULT_FALLBACKS.get(name, "")
    return _PLACEHOLDER_RE.sub(repl, tpl or "")


def render_system(
    template: PromptTemplate,
    ctx: dict,
    docs_map: dict[str, tuple[str, int | None]],
    doc_refs: Iterable[DocInjectSpec] = (),
) -> str:
    """
    docs_map: {doc_key: (content, version_id)}，由 Service 从 Store 取出后一次性传入。
    doc_refs: 决定哪些 doc 注入、注入顺序、是否强制、是否截断。
    """
    values: dict[str, str] = {}
    values.update(_builtin_vars())
    for k, v in (ctx or {}).items():
        if isinstance(v, (str, int, float)) or v is None:
            values[k] = "" if v is None else str(v)
        else:
            # 列表 / dict / datetime：落入字符串表示即可，不做复杂处理
            values[k] = str(v)

    body = _substitute(template.system or "", values)

    # 拼接 doc 注入块
    blocks: list[str] = []
    for spec in doc_refs or []:
        text, _ver = docs_map.get(spec.doc_key, ("", None))
        text = text.strip() if text else ""
        if not text:
            if spec.required:
                blocks.append(f"\n## {spec.title or spec.doc_key}\n(参考文档缺失)\n")
            continue
        text = _truncate(text, spec.max_chars)
        title = spec.title or spec.doc_key
        blocks.append(f"\n## {title}\n{text}\n")

    if blocks:
        # 旧 prompts.py 的注入位置在 system 首段之后、具体业务信息之前；
        # 为最大程度兼容，新模板推荐在 system 文本中用 {{doc_block}} 显式占位。
        # 若模板中包含 {{doc_block}}，则把 blocks 放到对应位置；否则追加到末尾。
        doc_block_text = "".join(blocks)
        if "{{doc_block}}" in (template.system or ""):
            body = body.replace("{{doc_block}}", doc_block_text)
        elif "{{ doc_block }}" in (template.system or ""):
            body = body.replace("{{ doc_block }}", doc_block_text)
        else:
            body = body.rstrip() + "\n" + doc_block_text
    else:
        # 清理未替换的 doc_block 占位
        body = body.replace("{{doc_block}}", "").replace("{{ doc_block }}", "")

    return body


def build_messages(system_text: str, history: list[dict] | None, query: str) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": system_text}]
    if history:
        msgs.extend(history)
    msgs.append({"role": "user", "content": query})
    return msgs
