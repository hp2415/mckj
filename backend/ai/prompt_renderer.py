"""
PromptRenderer：把 PromptTemplate + ctx + docs 渲染为最终 system 文本，并拼成 messages。

设计要点：
- 模板占位使用 {{var}} 形式（双大括号），避免与自然文本中的花括号冲突。
- ctx 中常用键：customer_card / ai_profile / order_summary / chat_summary /
  budget_amount / purchase_type / ai_history 等（见 ContextAssembler.assemble）。
  缺失时走 DEFAULT_FALLBACKS 兜底（参考旧 prompts.py 的行为：未知 / 暂无）。
- {{current_date}} 为内置变量，始终注入"今天的中文日期"。
- 话术时间规则：注入 season_label / time_context；仅对客户话术类模板在 system 末尾强制追加
  「打招呼用称呼+好、禁时段问候与节气、可按季节寒暄」。任务编排的 instruction（执行动作）不追加。
- doc 注入块：按 DocInjectSpec 的顺序拼在 system 末尾，标题前会加 "## "。
- max_chars: 只做"尾部省略"截断，避免复杂摘要；超长 doc 只保留前 max_chars 字符 + "…（已截断）"。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

from ai.prompt_models import PromptTemplate, DocInjectSpec
from ai.time_context import format_script_time_rules, time_context_vars


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
    "profile_tags_catalog": "",
    "profile_tag_catalog": "",
    "profile_tags_detail": "暂无动态标签",
    "customers_json": "[]",
    "task_cap": "15",
    "period_type_label": "",
    "sales_wechat_persona": "",
    "staff_identity": "未登记",
    "season_label": "",
    "season_hint": "",
    "time_context": "",
    "unit_season_context": "",
    "forbidden_period_greetings": "",
    "forbidden_solar_terms": "",
}

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_TIME_RULES_MARKER = "## 话术时间与打招呼硬性规则"
# 仅对「发给客户的话术」类模板强制追加；勿用 instruction 作线索（任务编排里也有该字段）
_SCRIPT_HINT_RE = re.compile(r"可直接复制|发给客户|发送给客户|微信消息|口播|称呼\s*\+\s*好|季节寒暄")
# 主线任务 instruction 是执行动作，不是客户话术，禁止追加季节寒暄规则
_ACTION_INSTRUCTION_HINT_RE = re.compile(r"不是话术")


def _builtin_vars(ref_date_text: str | None = None) -> dict[str, str]:
    vars_ = {
        "current_date": datetime.now().strftime("%Y年%m月%d日 %H:%M:%S"),
    }
    vars_.update(time_context_vars(ref_date_text=ref_date_text))
    return vars_


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


def _strip_script_time_rules(text: str) -> str:
    """移除已注入的话术时间规则块（含其后全文）。"""
    idx = text.find(_TIME_RULES_MARKER)
    if idx < 0:
        return text
    return text[:idx].rstrip()


def _ensure_script_time_rules(body: str, values: dict[str, str]) -> str:
    """客户话术类模板强制带上打招呼/季节硬规则（已发布旧模板同样生效）。"""
    text = (body or "").rstrip()
    # 任务编排等「instruction=执行动作」场景：不要注入/保留寒暄话术规则
    if _ACTION_INSTRUCTION_HINT_RE.search(text):
        text = _strip_script_time_rules(text)
        return text + ("\n" if text else "")
    if _TIME_RULES_MARKER in text:
        return text + ("\n" if text else "")
    if not _SCRIPT_HINT_RE.search(text):
        return text + ("\n" if text else "")
    rules = (values.get("time_context") or "").strip() or format_script_time_rules()
    if not text:
        return rules
    return text + "\n\n" + rules


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
    ctx = ctx or {}
    ref_date = ctx.get("current_date") or ctx.get("ref_today")
    ref_date_text = "" if ref_date is None else str(ref_date)

    values: dict[str, str] = {}
    values.update(_builtin_vars(ref_date_text=ref_date_text or None))
    for k, v in ctx.items():
        if isinstance(v, (str, int, float)) or v is None:
            values[k] = "" if v is None else str(v)
        else:
            # 列表 / dict / datetime：落入字符串表示即可，不做复杂处理
            values[k] = str(v)
    # 时间规则以代码为准；ctx 可覆盖 current_date，但季节/禁词仍按解析结果重算
    values.update(time_context_vars(ref_date_text=values.get("current_date") or ref_date_text or None))

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

    return _ensure_script_time_rules(body, values)


def render_auxiliary_doc_block(
    *,
    scenario_key: str,
    scenario_name: str,
    ctx: dict,
    docs_map: dict[str, tuple[str, int | None]],
    doc_refs: Iterable[DocInjectSpec] = (),
) -> str:
    """仅拼接辅场景的参考文档块，避免重复注入客户档案。"""
    blocks: list[str] = []
    for spec in doc_refs or []:
        text, _ver = docs_map.get(spec.doc_key, ("", None))
        text = text.strip() if text else ""
        if not text:
            continue
        text = _truncate(text, spec.max_chars)
        title = spec.title or spec.doc_key
        blocks.append(f"## {title}\n{text}\n")
    if not blocks:
        return ""
    heading = (scenario_key or scenario_name or "辅助场景").strip()
    return f"## 辅助场景：{heading}\n" + "".join(blocks)


def render_auxiliary_scenario_block(
    *,
    scenario_key: str,
    scenario_name: str,
    auxiliary_system: str,
    doc_block: str = "",
) -> str:
    """辅场景 system 片段：优先文档块，否则回退辅场景 published system。"""
    heading = (scenario_key or scenario_name or "辅助场景").strip()
    body = (doc_block or "").strip()
    if not body:
        body = (auxiliary_system or "").strip()
    if not body:
        return f"## 辅助场景：{heading}\n"
    return f"## 辅助场景：{heading}\n{body}\n"


def build_messages(system_text: str, history: list[dict] | None, query: str) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": system_text}]
    if history:
        msgs.extend(history)
    msgs.append({"role": "user", "content": query})
    return msgs
