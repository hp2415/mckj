import httpx
import json
from typing import Any, AsyncIterator, Optional

from core.logger import logger

# 流式首包慢（如部分 Qwen 路由）时，默认 90s 易被对端或客户端切断；非流式回退共用此配置
HTTP_TIMEOUT = httpx.Timeout(300.0, connect=30.0)

_STREAM_FALLBACK_ERRORS = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.LocalProtocolError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
)


def _delta_text(delta: dict) -> str:
    """兼容不同厂商的 delta.content：str、null、或 text part 列表。"""
    c = delta.get("content")
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for p in c:
            if isinstance(p, dict):
                if p.get("type") == "text" and "text" in p:
                    parts.append(str(p.get("text", "")))
                elif "text" in p:
                    parts.append(str(p.get("text", "")))
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    return str(c)


def _delta_reasoning(delta: dict) -> str:
    """DeepSeek thinking 模式流式字段：delta.reasoning_content。"""
    r = delta.get("reasoning_content")
    if r is None:
        return ""
    if isinstance(r, str):
        return r
    return str(r)


def _normalize_tool_calls_list(raw: Any) -> list:
    """SSE 里 tool_calls 可能是 list，或单条 dict（部分兼容网关）。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


def _merge_tool_delta(tool_calls_buffer: dict, tc: dict) -> None:
    """
    合并流式 tool_calls 片段。部分网关/模型不返回 index 或分多包补全 name/arguments。
    """
    try:
        idx = int(tc.get("index", 0))
    except (TypeError, ValueError):
        idx = 0
    if idx not in tool_calls_buffer:
        tool_calls_buffer[idx] = {"id": "", "name": "", "arguments": ""}
    if tc.get("id"):
        tool_calls_buffer[idx]["id"] = tc["id"]
    fn = tc.get("function")
    if isinstance(fn, dict):
        if fn.get("name"):
            tool_calls_buffer[idx]["name"] = fn["name"]
        ap = fn.get("arguments")
        if ap:
            tool_calls_buffer[idx]["arguments"] += str(ap)


class LLMClient:
    """统一 LLM 调用客户端 (OpenAI-compatible 协议)"""

    def __init__(self, api_url: str, api_key: str, model: str = "qwen-max"):
        self.api_url = api_url    # 如 https://dashscope.aliyuncs.com/compatible-mode/v1
        self.api_key = api_key
        self.model = model

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: list[dict],
        *,
        stream: bool,
        temperature: float,
        max_tokens: int,
        tools: Optional[list[dict]],
    ) -> dict[str, Any]:
        p: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            p["tools"] = tools
            # 显式 auto：少数 OpenAI 兼容网关默认行为与预期不一致
            p["tool_choice"] = "auto"
        else:
            model_l = (self.model or "").lower()
            if "deepseek" in model_l:
                # 百炼 deepseek-v4-pro 等默认开启 thinking，流式常长时间只推 reasoning_content。
                # 普通对话（无 tools）关闭思考，与 Qwen 一样直接流式输出 content。
                p["enable_thinking"] = False
                p["thinking"] = {"type": "disabled"}
        return p

    async def _consume_sse_stream(self, response: httpx.Response) -> AsyncIterator[str]:
        tool_calls_buffer: dict[int, dict] = {}
        text_chunks = 0
        reasoning_buffer = ""

        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                data = json.loads(data_str)
                choices = data.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                # 少数厂商（含部分 DeepSeek 路由）在流式最后一帧把完整 message 挂在 choice 上
                msg = choice.get("message") or {}

                # DeepSeek thinking：reasoning_content 与 content 可能在同一 delta 分帧到达，需分别处理
                for reasoning_piece in (
                    _delta_reasoning(delta),
                    _delta_reasoning(msg),
                ):
                    if reasoning_piece:
                        reasoning_buffer += reasoning_piece

                piece = _delta_text(delta)
                if not piece:
                    piece = _delta_text(msg)
                if not piece:
                    legacy = choice.get("text")
                    if isinstance(legacy, str) and legacy:
                        piece = legacy
                if piece:
                    text_chunks += 1
                    yield piece

                tclist = _normalize_tool_calls_list(delta.get("tool_calls"))
                if not tclist:
                    tclist = _normalize_tool_calls_list(msg.get("tool_calls"))
                for tc in tclist:
                    _merge_tool_delta(tool_calls_buffer, tc)

            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue

        if reasoning_buffer:
            yield f"__REASONING_CONTENT__:{reasoning_buffer}"

        if tool_calls_buffer:
            for _, tc in sorted(tool_calls_buffer.items(), key=lambda x: x[0]):
                yield f"__TOOL_CALL__:{json.dumps(tc, ensure_ascii=False)}"
        elif text_chunks == 0 and not reasoning_buffer:
            logger.warning(
                "LLM 流式结束但未解析到文本片段且无 tool_calls（model={}），"
                "可能是 delta 格式与解析器不兼容或上游返回空 choices",
                self.model,
            )

    async def _iter_from_nonstream(
        self,
        url: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        tools: Optional[list[dict]],
    ) -> AsyncIterator[str]:
        """流式被对端掐断时，用同参数非流式再请求一次，产出与 stream_chat 相同形式的片段。"""
        payload = self._payload(messages, stream=False, temperature=temperature, max_tokens=max_tokens, tools=tools)
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
            if resp.status_code != 200:
                raise Exception(f"LLM API Error ({resp.status_code}): {resp.text}")
            data = resp.json()

        choices = data.get("choices") or []
        if not choices:
            logger.warning("LLM 非流式返回无 choices（model={}）", self.model)
            return
        msg = choices[0].get("message") or {}
        # DeepSeek thinking 模式会返回 reasoning_content，且在后续 tool 回合要求原样回传。
        reasoning_content = msg.get("reasoning_content")
        if reasoning_content:
            yield f"__REASONING_CONTENT__:{str(reasoning_content)}"
        raw_content = msg.get("content")
        if raw_content:
            if isinstance(raw_content, str):
                if raw_content:
                    yield raw_content
            else:
                piece = _delta_text({"content": raw_content})
                if piece:
                    yield piece

        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            stub = {
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "arguments": fn.get("arguments") or "",
            }
            yield f"__TOOL_CALL__:{json.dumps(stub, ensure_ascii=False)}"

    async def stream_chat(
        self,
        messages: list[dict],    # 标准 OpenAI messages 格式
        temperature: float = 0.7,
        max_tokens: int = 1024,
        tools: list[dict] = None # OpenAI 格式的 tools
    ) -> AsyncIterator[str]:
        """
        调用 LLM Chat Completions 接口 (SSE 流式)。
        支持 tools 解析，如果识别到 function call，会将其作为特殊的 JSON string yield 给上层。
        """
        url = f"{self.api_url.rstrip('/')}/chat/completions"
        payload = self._payload(messages, stream=True, temperature=temperature, max_tokens=max_tokens, tools=tools)

        # DeepSeek 等：流式下 tool_calls 常不完整或只出现在非流式 message 中，导致模型仅输出「已修改」却无工具调用。
        # 对 DeepSeek 家族在携带 tools 时走非流式，保证 message.tool_calls 可被解析。
        model_l = (self.model or "").lower()
        if tools and "deepseek" in model_l:
            async for chunk in self._iter_from_nonstream(url, messages, temperature, max_tokens, tools):
                yield chunk
            return

        stream_had_text = False
        stream_had_tool = False
        stream_had_reasoning = False
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                async with client.stream("POST", url, json=payload, headers=self._headers()) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        raise Exception(f"LLM API Error ({response.status_code}): {error_body.decode()}")

                    async for chunk in self._consume_sse_stream(response):
                        if chunk.startswith("__TOOL_CALL__:"):
                            stream_had_tool = True
                        elif chunk.startswith("__REASONING_CONTENT__:"):
                            stream_had_reasoning = True
                        else:
                            stream_had_text = True
                        yield chunk

            # 兜底：流式仅有 reasoning（DeepSeek thinking 常见）或完全空包时，非流式补正文/tool_calls。
            if not stream_had_text and not stream_had_tool:
                logger.warning(
                    "LLM 流式未得到可用正文{}，已自动回退非流式重试 model={}",
                    "（仅有 reasoning_content）" if stream_had_reasoning else "",
                    self.model,
                )
                async for chunk in self._iter_from_nonstream(url, messages, temperature, max_tokens, tools):
                    if chunk.startswith("__REASONING_CONTENT__:") and stream_had_reasoning:
                        continue
                    yield chunk

        except _STREAM_FALLBACK_ERRORS as e:
            if stream_had_text or stream_had_tool:
                logger.error(
                    "LLM 流式中途断开且已有输出，放弃非流式整段重试以免重复 model={} err={}",
                    self.model,
                    e,
                )
                raise
            logger.warning(
                "LLM 流式传输中断（常见于上游 ~60s 闲置断开或部分 Qwen 流路由不稳），"
                "已改用非流式重试 model={} err={}",
                self.model,
                e,
            )
            async for chunk in self._iter_from_nonstream(url, messages, temperature, max_tokens, tools):
                if chunk.startswith("__REASONING_CONTENT__:") and stream_had_reasoning:
                    continue
                yield chunk

    async def chat(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 1024, tools: list[dict] = None) -> dict:
        """非流式调用，用于更稳健的 Function Calling 意图识别"""
        url = f"{self.api_url.rstrip('/')}/chat/completions"
        payload = self._payload(messages, stream=False, temperature=temperature, max_tokens=max_tokens, tools=tools)

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
            if resp.status_code != 200:
                raise Exception(f"LLM API Error: {resp.text}")
            return resp.json()
