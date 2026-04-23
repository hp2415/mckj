"""
桌面/API 可选对话模型列表：由 system_configs.llm_chat_models_list 配置（管理后台维护）。
格式（推荐用分号分隔多条，避免显示名里的逗号歧义）：
  模型ID:显示名;模型ID2:显示名2
也支持单段内用英文逗号分隔多条「含冒号」的项。
无配置或解析为空时，使用内置默认列表。
"""
from __future__ import annotations

from typing import Optional

DEFAULT_LLM_CHAT_MODEL_ENTRIES: list[tuple[str, str]] = [
    ("qwen3.5-plus", "通义千问 3.5 Plus"),
    ("deepseek-v3.2", "DeepSeek V3.2"),
    ("gpt-5.4", "GPT-5.4"),
]


def parse_llm_chat_models_list(value: Optional[str]) -> list[tuple[str, str]]:
    raw = (value or "").strip()
    if not raw:
        return list(DEFAULT_LLM_CHAT_MODEL_ENTRIES)

    if ";" in raw or "\n" in raw:
        chunks: list[str] = []
        for line in raw.splitlines():
            chunks.extend([x.strip() for x in line.split(";") if x.strip()])
    else:
        chunks = [x.strip() for x in raw.split(",") if x.strip()]

    out: list[tuple[str, str]] = []
    for part in chunks:
        if ":" in part:
            mid, lbl = part.split(":", 1)
            mid, lbl = mid.strip(), lbl.strip()
            if mid:
                out.append((mid, lbl or mid))
        elif part:
            out.append((part, part))

    return out if out else list(DEFAULT_LLM_CHAT_MODEL_ENTRIES)


def allowed_chat_model_ids(config_map: dict) -> frozenset[str]:
    return frozenset(m for m, _ in parse_llm_chat_models_list(config_map.get("llm_chat_models_list")))


def default_chat_model_id(config_map: dict) -> str:
    entries = parse_llm_chat_models_list(config_map.get("llm_chat_models_list"))
    return entries[0][0]


def chat_models_for_api_payload(config_map: dict) -> list[dict[str, str]]:
    """供 /api/system/configs_dict 返回给桌面端。"""
    return [{"id": mid, "label": lbl} for mid, lbl in parse_llm_chat_models_list(config_map.get("llm_chat_models_list"))]
