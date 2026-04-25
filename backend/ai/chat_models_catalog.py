"""
桌面/API 可选对话模型列表：由 system_configs.llm_chat_models_list 配置（管理后台维护）。
格式（推荐用分号分隔多条，避免显示名里的逗号歧义）：
  模型ID:显示名;模型ID2:显示名2
也支持单段内用英文逗号分隔多条「含冒号」的项。
无配置或解析为空时，使用内置默认列表。

2026-04：支持“模型 ↔ URL”路由：
- 推荐 JSON 格式（可扩展、避免分隔符歧义）：
  [
    {"id":"qwen3.5-plus","label":"通义千问 3.5 Plus","api_url":"...","api_key":"..."},
    {"id":"deepseek-v3.2","label":"DeepSeek V3.2","api_url":"..."}
  ]
  其中 api_key 可省略：省略时回退使用 system_configs.llm_api_key。
- 兼容旧格式（仅 id/label，无 url/key）。
"""
from __future__ import annotations

import json
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

    # JSON list of objects: [{"id":"...","label":"...","api_url":"...","api_key":"..."}, ...]
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                out: list[tuple[str, str]] = []
                for it in arr:
                    if not isinstance(it, dict):
                        continue
                    mid = str(it.get("id") or "").strip()
                    if not mid:
                        continue
                    lbl = str(it.get("label") or "").strip() or mid
                    out.append((mid, lbl))
                return out if out else list(DEFAULT_LLM_CHAT_MODEL_ENTRIES)
        except Exception:
            # fallthrough to legacy parser
            pass

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


def parse_llm_chat_model_routes(value: Optional[str]) -> dict[str, dict[str, str]]:
    """
    解析 model → {api_url?, api_key?} 的路由表。

    仅 JSON 格式支持 url/key；旧格式直接返回空 dict（走全局 llm_api_url/llm_api_key）。
    """
    raw = (value or "").strip()
    if not raw or not raw.startswith("["):
        return {}
    try:
        arr = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(arr, list):
        return {}
    routes: dict[str, dict[str, str]] = {}
    for it in arr:
        if not isinstance(it, dict):
            continue
        mid = str(it.get("id") or "").strip()
        if not mid:
            continue
        api_url = str(it.get("api_url") or it.get("url") or "").strip()
        api_key = str(it.get("api_key") or it.get("key") or "").strip()
        entry: dict[str, str] = {}
        if api_url:
            entry["api_url"] = api_url
        if api_key:
            entry["api_key"] = api_key
        if entry:
            routes[mid] = entry
    return routes


def resolve_chat_model_endpoint(config_map: dict, model_id: str) -> tuple[str, str]:
    """
    根据 system_configs.llm_chat_models_list（JSON）解析每个模型的专属 api_url/api_key；
    若该模型未配置，则回退到全局 llm_api_url / llm_api_key。
    """
    global_url = (config_map.get("llm_api_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1").strip()
    global_key = (config_map.get("llm_api_key") or "").strip()
    routes = parse_llm_chat_model_routes(config_map.get("llm_chat_models_list"))
    r = routes.get((model_id or "").strip()) or {}
    return (r.get("api_url") or global_url, r.get("api_key") or global_key)


def allowed_chat_model_ids(config_map: dict) -> frozenset[str]:
    return frozenset(m for m, _ in parse_llm_chat_models_list(config_map.get("llm_chat_models_list")))


def default_chat_model_id(config_map: dict) -> str:
    entries = parse_llm_chat_models_list(config_map.get("llm_chat_models_list"))
    return entries[0][0]


def chat_models_for_api_payload(config_map: dict) -> list[dict[str, str]]:
    """供 /api/system/configs_dict 返回给桌面端。"""
    return [{"id": mid, "label": lbl} for mid, lbl in parse_llm_chat_models_list(config_map.get("llm_chat_models_list"))]
