import asyncio
import httpx
import json
import os
import time
from typing import Any, AsyncIterator, Optional

from core.logger import logger
from ai.llm_usage import LLMUsageContext, parse_usage_fields, schedule_log_llm_usage

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

_shared_http_client: httpx.AsyncClient | None = None
_llm_semaphore: asyncio.Semaphore | None = None


def _llm_concurrency_limit() -> int:
    try:
        return max(1, int(os.getenv("LLM_MAX_CONCURRENT") or "16"))
    except ValueError:
        return 16


def get_llm_semaphore() -> asyncio.Semaphore:
    global _llm_semaphore
    if _llm_semaphore is None:
        _llm_semaphore = asyncio.Semaphore(_llm_concurrency_limit())
    return _llm_semaphore


def get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None or _shared_http_client.is_closed:
        _shared_http_client = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
        )
    return _shared_http_client


async def close_shared_http_client() -> None:
    global _shared_http_client
    if _shared_http_client is not None and not _shared_http_client.is_closed:
        await _shared_http_client.aclose()
    _shared_http_client = None


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
            p["tool_choice"] = "auto"
        else:
            model_l = (self.model or "").lower()
            if "deepseek" in model_l:
                p["enable_thinking"] = False
                p["thinking"] = {"type": "disabled"}
        if stream:
            # OpenAI 兼容：流式最后一帧可能携带 usage
            p["stream_options"] = {"include_usage": True}
        return p

    def _record_usage(
        self,
        *,
        usage: LLMUsageContext | None,
        usage_dict: dict[str, Any] | None,
        duration_ms: int,
        stream_mode: str,
        fallback_reason: str | None = None,
    ) -> None:
        pt, ct, tt = parse_usage_fields(usage_dict)
        extra = dict(usage.extra) if usage and usage.extra else None
        schedule_log_llm_usage(
            model=self.model,
            api_url=self.api_url,
            scenario_key=usage.scenario_key if usage else None,
            user_id=usage.user_id if usage else None,
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=tt,
            duration_ms=duration_ms,
            stream_mode=stream_mode,
            fallback_reason=fallback_reason,
            extra=extra,
        )

    async def _consume_sse_stream(
        self,
        response: httpx.Response,
        usage_out: dict[str, Any],
    ) -> AsyncIterator[str]:
        tool_calls_buffer: dict[int, dict] = {}
        text_chunks = 0
        reasoning_buffer = ""
        streamed_chars = 0

        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                data = json.loads(data_str)
                if isinstance(data.get("usage"), dict):
                    usage_out.update(data["usage"])
                choices = data.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                msg = choice.get("message") or {}

                for reasoning_piece in (
                    _delta_reasoning(delta),
                    _delta_reasoning(msg),
                ):
                    if reasoning_piece:
                        reasoning_buffer += reasoning_piece

                piece = _delta_text(delta)
                if piece:
                    text_chunks += 1
                    streamed_chars += len(piece)
                    yield piece
                else:
                    msg_piece = _delta_text(msg)
                    if msg_piece:
                        if len(msg_piece) > streamed_chars:
                            tail = msg_piece[streamed_chars:]
                            if tail:
                                text_chunks += 1
                                streamed_chars = len(msg_piece)
                                yield tail
                        else:
                            text_chunks += 1
                            streamed_chars += len(msg_piece)
                            yield msg_piece
                    else:
                        legacy = choice.get("text")
                        if isinstance(legacy, str) and legacy:
                            text_chunks += 1
                            streamed_chars += len(legacy)
                            yield legacy

                tclist = _normalize_tool_calls_list(delta.get("tool_calls"))
                if not tclist:
                    tclist = _normalize_tool_calls_list(msg.get("tool_calls"))
                for tc in tclist:
                    _merge_tool_delta(tool_calls_buffer, tc)

            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue

        # 排空连接尾部，避免上游提前关闭时丢失最后一行 SSE
        try:
            await response.aread()
        except Exception:
            pass

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

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], int, dict[str, Any]]:
        client = get_shared_http_client()
        t0 = time.perf_counter()
        async with get_llm_semaphore():
            resp = await client.post(url, json=payload, headers=self._headers())
        duration_ms = int((time.perf_counter() - t0) * 1000)
        if resp.status_code != 200:
            raise Exception(f"LLM API Error ({resp.status_code}): {resp.text}")
        data = resp.json()
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return data, duration_ms, usage

    async def _yield_nonstream_chunks(self, data: dict[str, Any]) -> AsyncIterator[str]:
        choices = data.get("choices") or []
        if not choices:
            logger.warning("LLM 非流式返回无 choices（model={}）", self.model)
            return
        msg = choices[0].get("message") or {}
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

    async def _iter_from_nonstream(
        self,
        url: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        tools: Optional[list[dict]],
        *,
        usage: LLMUsageContext | None = None,
        fallback_reason: str | None = None,
    ) -> AsyncIterator[str]:
        payload = self._payload(
            messages, stream=False, temperature=temperature, max_tokens=max_tokens, tools=tools
        )
        data, duration_ms, usage_dict = await self._post_json(url, payload)
        self._record_usage(
            usage=usage,
            usage_dict=usage_dict,
            duration_ms=duration_ms,
            stream_mode="nonstream",
            fallback_reason=fallback_reason,
        )
        async for chunk in self._yield_nonstream_chunks(data):
            yield chunk

    async def stream_chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        tools: list[dict] = None,
        usage: LLMUsageContext | None = None,
    ) -> AsyncIterator[str]:
        """
        调用 LLM Chat Completions 接口 (SSE 流式)。
        支持 tools 解析；usage 参数用于 token 计量落库。
        """
        url = f"{self.api_url.rstrip('/')}/chat/completions"
        payload = self._payload(
            messages, stream=True, temperature=temperature, max_tokens=max_tokens, tools=tools
        )

        model_l = (self.model or "").lower()
        if tools and "deepseek" in model_l:
            async for chunk in self._iter_from_nonstream(
                url, messages, temperature, max_tokens, tools,
                usage=usage,
                fallback_reason="deepseek_tools_nonstream",
            ):
                yield chunk
            return

        stream_had_text = False
        stream_had_tool = False
        stream_had_reasoning = False
        usage_holder: dict[str, Any] = {}
        t0 = time.perf_counter()
        try:
            client = get_shared_http_client()
            async with get_llm_semaphore():
                async with client.stream("POST", url, json=payload, headers=self._headers()) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        raise Exception(f"LLM API Error ({response.status_code}): {error_body.decode()}")

                    async for chunk in self._consume_sse_stream(response, usage_holder):
                        if chunk.startswith("__TOOL_CALL__:"):
                            stream_had_tool = True
                        elif chunk.startswith("__REASONING_CONTENT__:"):
                            stream_had_reasoning = True
                        else:
                            stream_had_text = True
                        yield chunk

            duration_ms = int((time.perf_counter() - t0) * 1000)
            self._record_usage(
                usage=usage,
                usage_dict=usage_holder,
                duration_ms=duration_ms,
                stream_mode="stream",
            )

            if not stream_had_text and not stream_had_tool:
                logger.warning(
                    "LLM 流式未得到可用正文{}，已自动回退非流式重试 model={}",
                    "（仅有 reasoning_content）" if stream_had_reasoning else "",
                    self.model,
                )
                async for chunk in self._iter_from_nonstream(
                    url, messages, temperature, max_tokens, tools,
                    usage=usage,
                    fallback_reason="stream_empty_retry",
                ):
                    if chunk.startswith("__REASONING_CONTENT__:") and stream_had_reasoning:
                        continue
                    yield chunk

        except _STREAM_FALLBACK_ERRORS as e:
            if stream_had_text or stream_had_tool:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                self._record_usage(
                    usage=usage,
                    usage_dict=usage_holder,
                    duration_ms=duration_ms,
                    stream_mode="stream",
                    fallback_reason="stream_aborted_mid_output",
                )
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
            async for chunk in self._iter_from_nonstream(
                url, messages, temperature, max_tokens, tools,
                usage=usage,
                fallback_reason=f"stream_transport_error:{type(e).__name__}",
            ):
                if chunk.startswith("__REASONING_CONTENT__:") and stream_had_reasoning:
                    continue
                yield chunk

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        tools: list[dict] = None,
        usage: LLMUsageContext | None = None,
    ) -> dict:
        """非流式调用，用于更稳健的 Function Calling 意图识别"""
        url = f"{self.api_url.rstrip('/')}/chat/completions"
        payload = self._payload(
            messages, stream=False, temperature=temperature, max_tokens=max_tokens, tools=tools
        )
        data, duration_ms, usage_dict = await self._post_json(url, payload)
        self._record_usage(
            usage=usage,
            usage_dict=usage_dict,
            duration_ms=duration_ms,
            stream_mode="nonstream",
        )
        return data
