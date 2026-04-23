import httpx
import json
from typing import AsyncIterator

class LLMClient:
    """统一 LLM 调用客户端 (OpenAI-compatible 协议)"""

    def __init__(self, api_url: str, api_key: str, model: str = "qwen-max"):
        self.api_url = api_url    # 如 https://dashscope.aliyuncs.com/compatible-mode/v1
        self.api_key = api_key
        self.model = model

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
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    raise Exception(f"LLM API Error ({response.status_code}): {error_body.decode()}")

                tool_calls_buffer = {}

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        delta = data["choices"][0].get("delta", {})
                        
                        # 解析普通文本
                        content = delta.get("content", "")
                        if content:
                            yield content
                            
                        # 解析 Tool Calls
                        if "tool_calls" in delta:
                            for tc in delta["tool_calls"]:
                                idx = tc["index"]
                                if idx not in tool_calls_buffer:
                                    tool_calls_buffer[idx] = {
                                        "id": tc.get("id", ""),
                                        "name": tc.get("function", {}).get("name", ""),
                                        "arguments": ""
                                    }
                                if "function" in tc and "arguments" in tc["function"]:
                                    tool_calls_buffer[idx]["arguments"] += tc["function"]["arguments"]
                                    
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

                # 如果有完整的 tool_calls，将其作为特殊的结构体 yield 出去
                if tool_calls_buffer:
                    for tc in tool_calls_buffer.values():
                        # 为了避免混淆，使用特定前缀
                        yield f"__TOOL_CALL__:{json.dumps(tc, ensure_ascii=False)}"

    async def chat(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 1024, tools: list[dict] = None) -> dict:
        """非流式调用，用于更稳健的 Function Calling 意图识别"""
        url = f"{self.api_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise Exception(f"LLM API Error: {resp.text}")
            return resp.json()
