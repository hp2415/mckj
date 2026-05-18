from __future__ import annotations

from typing import Any

from ai.prompt_models import PromptParams

DEEPSEEK_PLAIN_WECHAT_SYSTEM_SUFFIX = (
    "\n\n【本模型输出硬性要求（覆盖上文示例风格）】\n"
    "只输出一条可直接复制到微信发给客户的纯文本，不要用 Markdown："
    "禁止井号标题、星号加粗、要点列表、分隔线、代码块、表格。\n"
    "字数控制在150字以内。不要写「以下是」「供您参考」「方案如下」等说明，"
    "不要分段标题，不要给多版本备选。"
)


def is_deepseek_model(model: str) -> bool:
    return "deepseek" in (model or "").lower()


def wants_plain_wechat_output(*, is_real_customer: bool, tools_enabled: bool) -> bool:
    return bool(is_real_customer and not tools_enabled)


def apply_model_output_constraints(
    messages: list[dict],
    *,
    model: str,
    is_real_customer: bool,
    tools_enabled: bool,
) -> list[dict]:
    if not wants_plain_wechat_output(is_real_customer=is_real_customer, tools_enabled=tools_enabled):
        return messages
    if not is_deepseek_model(model):
        return messages
    if not messages or messages[0].get("role") != "system":
        return messages

    system_text = str(messages[0].get("content") or "")
    if "【本模型输出硬性要求" in system_text:
        return messages

    patched = list(messages)
    patched[0] = {
        **messages[0],
        "content": system_text.rstrip() + DEEPSEEK_PLAIN_WECHAT_SYSTEM_SUFFIX,
    }
    return patched


def resolve_llm_call_params(
  params: PromptParams,
  *,
  model: str,
  is_real_customer: bool,
  tools_enabled: bool,
) -> dict[str, Any]:
    temperature = 0.7 if params.temperature is None else float(params.temperature)
    max_tokens = 1024 if params.max_tokens is None else int(params.max_tokens)

    if (
        params.max_tokens is None
        and wants_plain_wechat_output(is_real_customer=is_real_customer, tools_enabled=tools_enabled)
        and is_deepseek_model(model)
    ):
        max_tokens = 384

    return {"temperature": temperature, "max_tokens": max_tokens}
