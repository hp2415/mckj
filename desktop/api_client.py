import os
import json
import httpx
import hashlib
import contextlib
import time
import asyncio
from typing import Optional
from urllib.parse import quote, urlparse
from PySide6.QtCore import QObject, Signal
from perf_timing import enabled as _perf_enabled, log as _perf_log

@contextlib.asynccontextmanager
async def _dummy_client(client, timeout=None):
    """包装共享 AsyncClient，按请求传入 timeout，避免并行 gather 互相覆盖 client.timeout。"""
    yield _TimeoutClient(client, timeout if timeout is not None else cfg.timeout)


def _http_path(url) -> str:
    try:
        return urlparse(str(url)).path or str(url)
    except Exception:
        return str(url)


class _TimeoutClient:
    __slots__ = ("_client", "_timeout")

    def __init__(self, client, timeout):
        self._client = client
        self._timeout = timeout

    def _t(self, timeout):
        return timeout if timeout is not None else self._timeout

    async def _timed(self, method: str, url, coro):
        if not _perf_enabled():
            return await coro
        t0 = time.perf_counter()
        status = "-"
        try:
            resp = await coro
            status = getattr(resp, "status_code", "-")
            return resp
        finally:
            _perf_log(
                f"http.{method}",
                (time.perf_counter() - t0) * 1000.0,
                force=True,
                path=_http_path(url),
                status=status,
            )

    async def get(self, url, *args, timeout=None, **kwargs):
        return await self._timed(
            "GET", url, self._client.get(url, *args, timeout=self._t(timeout), **kwargs)
        )

    async def post(self, url, *args, timeout=None, **kwargs):
        return await self._timed(
            "POST", url, self._client.post(url, *args, timeout=self._t(timeout), **kwargs)
        )

    async def put(self, url, *args, timeout=None, **kwargs):
        return await self._timed(
            "PUT", url, self._client.put(url, *args, timeout=self._t(timeout), **kwargs)
        )

    async def patch(self, url, *args, timeout=None, **kwargs):
        return await self._timed(
            "PATCH", url, self._client.patch(url, *args, timeout=self._t(timeout), **kwargs)
        )

    async def delete(self, url, *args, timeout=None, **kwargs):
        return await self._timed(
            "DELETE", url, self._client.delete(url, *args, timeout=self._t(timeout), **kwargs)
        )

    async def request(self, method, url, *args, timeout=None, **kwargs):
        return await self._timed(
            str(method or "").upper(),
            url,
            self._client.request(method, url, *args, timeout=self._t(timeout), **kwargs),
        )

    def stream(self, *args, timeout=None, **kwargs):
        return self._client.stream(*args, timeout=self._t(timeout), **kwargs)

from storage import SecureStorage
from logger_cfg import logger
from config_loader import cfg

class APIClient(QObject):
    """
    桌面端核心通讯器。
    集成了：JWT 内存化管理、基于用户隔离的本地加密缓存、以及自动重连/重试机制。
    """
    unauthorized = Signal()

    def __init__(self, base_url: str = None):
        super().__init__()
        # 优先采用传入参数，否则使用配置负载
        self.base_url = (base_url or cfg.api_url).rstrip("/")
        self.token = None          # 令牌始终仅在内存中持有，不落盘
        self.user_data = None      # 存放当前登录用户的元数据
        self.storage = None        # 根据登录用户动态加载的加密存储库
        
        # 共享持久连接池
        self.client = httpx.AsyncClient()
        # 同一海报并发 ensure 时复用同一次下载
        self._media_inflight: dict[str, asyncio.Future] = {}

    async def aclose(self):
        """应用退出时释放底层 HTTP 连接池。"""
        try:
            if self.client:
                await self.client.aclose()
        except Exception:
            pass

    def _generate_cache_key(self, endpoint: str, **params) -> str:
        """根据路径和参数生成唯一的哈希键，防止文件名非法字符"""
        query_str = json.dumps(params, sort_keys=True)
        return hashlib.sha256(f"{endpoint}_{query_str}".encode()).hexdigest()

    def _check_auth(self, response: httpx.Response):
        """检查响应状态码，如果是 401 则触发未授权信号"""
        if response.status_code == 401:
            logger.warning(f"检测到令牌失效 (401): {response.url}")
            self.unauthorized.emit()
        return response

    def _parse_json(self, resp: httpx.Response):
        self._check_auth(resp)
        try:
            data = resp.json()
        except Exception:
            data = None
        if resp.status_code >= 400:
            detail = ""
            if isinstance(data, dict):
                detail = data.get("detail") or data.get("message") or ""
                if data.get("code") and data.get("code") != 200:
                    return data
            if isinstance(detail, list):
                detail = "; ".join(str(x) for x in detail)
            return {
                "code": resp.status_code,
                "message": str(detail or resp.text or f"HTTP {resp.status_code}"),
                "data": None,
            }
        return data if data is not None else {"code": 500, "message": "无效响应", "data": None}

    async def login(self, username, password):
        """对接 FastAPI 后端登录逻辑"""
        url = f"{self.base_url}/api/auth/login"
        payload = {"username": username, "password": password}
        try:
            # 采用 x-www-form-urlencoded 格式发送登录请求
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                response = await client.post(url, data=payload)
                if response.status_code == 200:
                    data = response.json()
                    self.token = data["access_token"]
                    self.user_data = data
                    # 登录成功后，立即激活基于 user_id 的加密存储
                    self.storage = SecureStorage(data["user_id"])
                    # 自动尝试同步绑定（幂等；失败不影响登录）
                    try:
                        headers = {"Authorization": f"Bearer {self.token}"}
                        await client.post(
                            f"{self.base_url}/api/me/sales-wechats/auto-bind",
                            headers=headers,
                            timeout=10.0,
                        )
                    except Exception:
                        pass
                    return True, "登录成功"
                else:
                    detail = response.json().get("detail", "账号或密码错误")
                    return False, detail
        except Exception as e:
            return False, f"无法连接到服务器: {str(e)}"

    async def search_products(self, keyword: str = "", supplier_name: str = "", 
                              cat1: str = "", cat2: str = "", cat3: str = "", 
                              province: str = "", city: str = "", district: str = "",
                              min_price: float = None, max_price: float = None, skip: int = 0, limit: int = 20):
        """
        直接请求后端商品搜索接口，支持高阶过滤参数。
        """
        if not self.token:
            return None

        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.base_url}/api/product/search"
        params = {
            "keyword": keyword, 
            "supplier_name": supplier_name,
            "cat1": cat1,
            "cat2": cat2,
            "cat3": cat3,
            "province": province,
            "city": city,
            "district": district,
            "min_price": min_price,
            "max_price": max_price,
            "skip": skip, 
            "limit": limit
        }
        
        # 5.5 参数清洗：移除 None 和空字符串，防止后端 FastAPI 报 422 校验错误
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        
        headers = {"Authorization": f"Bearer {self.token}"}

        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.get(url, params=params, headers=headers)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json()
                return None
        except Exception as e:
            logger.warning(f"搜索商品请求异常: {e}")
            return None

    async def get_product_metadata(self, supplier_name: str = None):
        """获取商品筛选元数据 (供应商和分类树)，支持按店铺过滤"""
        if not self.token: return None
        url = f"{self.base_url}/api/product/metadata"
        headers = {"Authorization": f"Bearer {self.token}"}
        params = {"supplier_name": supplier_name} if supplier_name else {}
        
        try:
            async with _dummy_client(self.client, timeout=10.0) as client:
                resp = await client.get(url, params=params, headers=headers)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json().get("data", {})
                return None
        except Exception as e:
            logger.warning(f"获取商品元数据异常: {e}")
            return None

    async def get_my_customers(self):
        """
        获取当前员工关联的所有客户记录，用于侧边栏展示。
        不使用本地缓存，确保列表的实时性。
        """
        if not self.token:
            return None
            
        url = f"{self.base_url}/api/customer/my"
        headers = {"Authorization": f"Bearer {self.token}"}
        # 全量客户列表偏重，单独放宽超时（不低于全局 Network.timeout）
        list_timeout = max(float(cfg.timeout), 45.0)
        
        try:
            async with _dummy_client(self.client, timeout=list_timeout) as client:
                resp = await client.get(url, headers=headers)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json()
                logger.warning(
                    "拉取客户列表失败: HTTP %s %s",
                    resp.status_code,
                    (resp.text or "")[:200],
                )
                return None
        except Exception as e:
            logger.warning(f"拉取客户列表异常: {type(e).__name__}: {e!r}")
            return None

    async def get_customer_detail(self, customer_id: str, sales_wechat_id: Optional[str] = None):
        """按需拉取客户详情（列表瘦身后画像全文不随 /my 返回）。"""
        if not self.token:
            return None
        url = f"{self.base_url}/api/customer/id/{customer_id}/detail"
        params = {}
        if sales_wechat_id:
            params["sales_wechat_id"] = sales_wechat_id
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=10.0) as client:
                resp = await client.get(url, params=params, headers=headers)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json()
                return None
        except Exception as e:
            logger.warning(f"拉取客户详情异常 (ID: {customer_id}): {e}")
            return None

    async def update_customer_relation(self, customer_phone: str, update_data: dict):
        """
        局部更新当前员工对指定客户的备注信息。
        """
        if not self.token:
            return None
            
        url = f"{self.base_url}/api/customer/relation"
        params = {"customer_phone": customer_phone}
        headers = {"Authorization": f"Bearer {self.token}"}
        
        try:
            async with _dummy_client(self.client, timeout=10.0) as client:
                resp = await client.patch(url, params=params, json=update_data, headers=headers)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            return {"code": 500, "message": str(e)}

    async def update_customer_full_info(
        self, customer_id: str, lookup_phone: Optional[str], update_data: dict
    ):
        """
        全面更新客户客观或主观面板数据。
        lookup_phone 为打开面板时的手机号（用于定位）；为空时用 customer_id 路由。
        """
        if not self.token:
            return None
        if lookup_phone:
            seg = quote(str(lookup_phone), safe="")
            url = f"{self.base_url}/api/customer/{seg}/info"
        else:
            url = f"{self.base_url}/api/customer/id/{customer_id}/info"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=10.0) as client:
                resp = await client.put(url, json=update_data, headers=headers)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            return {"code": 500, "message": str(e)}

    async def get_customer_orders(self, customer_id: str):
        """历史订单流水拉取 (基于 ID 绑定，规避换号风险)"""
        if not self.token: return None
        url = f"{self.base_url}/api/customer/orders/{customer_id}"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.warning(f"拉取客户订单流水异常 (ID: {customer_id}): {e}")
            return None
        return None

    async def get_profile_tag_options(self):
        """管理平台启用的客户动态标签（桌面多选）。"""
        if not self.token:
            return None
        url = f"{self.base_url}/api/customer/profile_tag_options"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.get(url, headers=headers)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.warning(f"拉取动态标签选项异常: {e}")
        return None

    async def get_configs_dict(self):
        """拉取系统级别下发的配置选项字典"""
        if not self.token: return {}
        url = f"{self.base_url}/api/system/configs_dict"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=5.0) as client:
                resp = await client.get(url, headers=headers)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json().get("data", {})
        except Exception as e:
            logger.error(f"无法获取配置字典项, backend 可能熔断或无网络: {e}")
            return {}

    async def stream_ai_chat(
        self,
        query: str,
        customer_phone: Optional[str] = None,
        raw_customer_id: Optional[str] = None,
        sales_wechat_id: Optional[str] = None,
        scenario: str = "general_chat",
        conversation_id: str = None,
        chat_model: str = None,
        proposal_model: str = None,
    ):
        """
        对接后端 AI 网关 SSE 流式接口 /api/ai/chat。
        """
        if not self.token:
            yield "Error: 未登录"
            return

        url = f"{self.base_url}/api/ai/chat"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        payload = {
            "query": query,
            "scenario": scenario,
        }
        if customer_phone:
            payload["customer_phone"] = customer_phone
        if raw_customer_id:
            payload["raw_customer_id"] = str(raw_customer_id).strip()
        if sales_wechat_id:
            payload["sales_wechat_id"] = str(sales_wechat_id).strip()
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if chat_model:
            payload["chat_model"] = chat_model
        if proposal_model:
            payload["proposal_model"] = proposal_model

        async with _dummy_client(self.client, timeout=300.0) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code == 401:
                    self.unauthorized.emit()
                    yield "Error: 登录已过期"
                    return
                if response.status_code != 200:
                    yield f"Error: 服务器响应异常 ({response.status_code})"
                    return

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        data = json.loads(data_str)
                        event = data.get("event")
                        if event == "chunk":
                            yield data.get("text", "")
                        elif event == "meta":
                            meta = {
                                "chat_model": data.get("chat_model", ""),
                                "scenario": data.get("scenario", ""),
                                "auxiliary_scenarios": data.get("auxiliary_scenarios") or [],
                                "scenarios": data.get("scenarios") or [],
                                "route": data.get("route") or {},
                            }
                            yield f"[META_MODEL:{json.dumps(meta, ensure_ascii=False)}]"
                        elif event == "done":
                            done_text = data.get("text")
                            if done_text:
                                yield f"[DONE_TEXT:{json.dumps(done_text, ensure_ascii=False)}]"
                            msg_id = data.get("msg_id")
                            if msg_id:
                                yield f"[MSG_ID:{msg_id}]"
                        elif event == "system_action":
                            if data.get("action") == "update_customer":
                                action_payload = data.get("changes", {})
                            else:
                                action_payload = data
                            yield f"[SYSTEM_ACTION:{json.dumps(action_payload, ensure_ascii=False)}]"
                        elif event == "error":
                            yield f"Error: {data.get('text', '未知错误')}"
                    except (json.JSONDecodeError, KeyError):
                        continue

                # 读完 SSE 后主动排空尾部，降低上游提前关连接时丢最后一包的概率
                try:
                    await response.aread()
                except Exception:
                    pass

    async def get_proposal(self, proposal_id: int):
        if not self.token:
            return None
        url = f"{self.base_url}/api/proposals/{int(proposal_id)}"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=15.0) as client:
                response = await client.get(url, headers=headers)
                self._check_auth(response)
                if response.status_code == 200:
                    return response.json().get("data")
        except Exception as e:
            logger.warning(f"查询方案状态失败 proposal_id={proposal_id}: {e}")
        return None

    async def revise_proposal(self, proposal_id: int, feedback: str):
        if not self.token:
            return None
        url = f"{self.base_url}/api/proposals/{int(proposal_id)}/revise"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=15.0) as client:
                response = await client.post(
                    url, json={"feedback": feedback}, headers=headers
                )
                self._check_auth(response)
                if response.status_code == 200:
                    return response.json().get("data")
                logger.warning(
                    f"提交方案调整失败 HTTP {response.status_code}: {response.text[:200]}"
                )
        except Exception as e:
            logger.warning(f"提交方案调整失败 proposal_id={proposal_id}: {e}")
        return None

    async def download_proposal(self, proposal_id: int, version: int, target_path: str) -> bool:
        if not self.token:
            return False
        url = f"{self.base_url}/api/proposals/{int(proposal_id)}/download"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=60.0) as client:
                response = await client.get(
                    url, params={"version": int(version)}, headers=headers
                )
                self._check_auth(response)
                if response.status_code != 200:
                    return False
                with open(target_path, "wb") as output:
                    output.write(response.content)
                return True
        except Exception as e:
            logger.warning(f"下载方案失败 proposal_id={proposal_id}: {e}")
            return False

    async def get_sync_status(self):
        """获取云端货源最后一次同步的时间与状态"""
        if not self.token:
            return {}
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=10.0) as client:
                resp = await client.get(f"{self.base_url}/api/system/sync/status", headers=headers)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.error(f"无法拉取后台同步探针状态: {e}")
            pass
        return {}

    async def get_ai_scenarios(self, chat_context: str = None):
        """拉取后端可用的 AI 场景列表（用于桌面端下拉框）。

        chat_context: "free" | "customer" | None（不传则返回两类桌面场景，不含 backend_only）
        """
        if not self.token:
            return None
        url = f"{self.base_url}/api/ai/scenarios"
        headers = {"Authorization": f"Bearer {self.token}"}
        params = {}
        if chat_context:
            params["chat_context"] = chat_context
        try:
            async with _dummy_client(self.client, timeout=5.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json()
                return None
        except Exception as e:
            logger.warning(f"拉取场景列表失败: {e}")
            return None

    async def trigger_sync_task(self):
        """手动触发后端全量同步 (需 Admin 权限)"""
        if not self.token:
            return {"code": 401, "msg": "未登录"}
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=10.0) as client:
                resp = await client.post(f"{self.base_url}/api/system/sync/trigger", headers=headers)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json()
                return {"code": resp.status_code, "msg": "请求失败"}
        except Exception as e:
            return {"code": 500, "msg": str(e)}

    async def upload_wechat_history(self, filepath: str):
        """上传微信对话历史 (CSV/Excel) 到服务端以建立上下文地基"""
        if not self.token:
            return {"code": 401, "msg": "未登录"}
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            import os
            # Ensure filepath exists
            if not os.path.exists(filepath):
                return {"code": 400, "message": "文件不存在"}
                
            filename = os.path.basename(filepath)
            with open(filepath, "rb") as f:
                file_bytes = f.read()
                
            files = {"file": (filename, file_bytes, "application/octet-stream")}
            
            async with _dummy_client(self.client, timeout=60.0) as client:
                resp = await client.post(f"{self.base_url}/api/customer/upload_wechat", headers=headers, files=files)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json()
                return {"code": resp.status_code, "msg": "请求失败或网络异常"}
        except Exception as e:
            return {"code": 500, "msg": f"上传异常: {str(e)}"}
            
    async def import_manual_followup(self, filepath: str):
        """导入本周需跟进的客户名单 (Excel/CSV)"""
        if not self.token:
            return {"code": 401, "msg": "未登录"}
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            import os
            if not os.path.exists(filepath):
                return {"code": 400, "message": "文件不存在"}
                
            filename = os.path.basename(filepath)
            with open(filepath, "rb") as f:
                file_bytes = f.read()
                
            files = {"file": (filename, file_bytes, "application/octet-stream")}
            
            async with _dummy_client(self.client, timeout=60.0) as client:
                resp = await client.post(f"{self.base_url}/api/customer/import_manual_followup", headers=headers, files=files)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json()
                return {"code": resp.status_code, "msg": "请求失败或网络异常"}
        except Exception as e:
            return {"code": 500, "msg": f"上传异常: {str(e)}"}
            
    async def clear_manual_followup(self):
        """清空本周手动导入的客户标签"""
        if not self.token:
            return {"code": 401, "msg": "未登录"}
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=10.0) as client:
                resp = await client.post(f"{self.base_url}/api/customer/clear_manual_followup", headers=headers)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json()
                return {"code": resp.status_code, "msg": "请求失败或网络异常"}
        except Exception as e:
            return {"code": 500, "msg": f"请求异常: {str(e)}"}

    async def save_chat_message(self, phone: str, role: str, content: str, convid: str = None, is_regen: bool = False):
        """保存单条对话记录到后端"""
        if not self.token: return None
        url = f"{self.base_url}/api/customer/{phone}/chat_message"
        headers = {"Authorization": f"Bearer {self.token}"}
        payload = {
            "role": role,
            "content": content,
            "dify_conv_id": convid,
            "is_regenerated": is_regen
        }
        try:
            async with _dummy_client(self.client, timeout=5.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.error(f"保存聊天记录到云端失败: {e}")
            return None

    async def get_chat_history(
        self,
        phone: str,
        limit: int = 20,
        skip: int = 0,
        sales_wechat_id: Optional[str] = None,
    ):
        """获取后端存储历史 AI 聊天记录（旧：按手机号）。"""
        if not self.token: return []
        url = f"{self.base_url}/api/customer/{phone}/chat_history"
        params = {"limit": limit, "skip": skip}
        if sales_wechat_id:
            params["sales_wechat_id"] = str(sales_wechat_id).strip()
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=5.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json().get("data", [])
                return []
        except Exception as e:
            logger.warning(f"拉取历史聊天记录异常: {e}")
            return []

    async def get_chat_history_by_id(
        self,
        raw_customer_id: str,
        limit: int = 20,
        skip: int = 0,
        sales_wechat_id: Optional[str] = None,
    ):
        """按 raw_customer_id 拉取历史聊天记录（推荐：不依赖手机号）。"""
        if not self.token:
            return []
        seg = quote(str(raw_customer_id), safe="")
        url = f"{self.base_url}/api/customer/id/{seg}/chat_history"
        params = {"limit": limit, "skip": skip}
        if sales_wechat_id:
            params["sales_wechat_id"] = str(sales_wechat_id).strip()
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=5.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json().get("data", [])
                return []
        except Exception as e:
            logger.warning(f"按ID拉取历史聊天记录异常 (ID: {raw_customer_id}): {e}")
            return []

    async def get_wechat_chat_logs_by_id(
        self,
        raw_customer_id: str,
        limit: int = 50,
        skip: int = 0,
        sales_wechat_id: Optional[str] = None,
    ):
        """按 raw_customer_id 拉取云客同步的微信原始聊天记录。"""
        if not self.token:
            return {"code": 401, "message": "未登录", "data": [], "has_more": False}
        seg = quote(str(raw_customer_id), safe="")
        url = f"{self.base_url}/api/customer/id/{seg}/wechat_chat_logs"
        params = {"limit": limit, "skip": skip}
        if sales_wechat_id:
            params["sales_wechat_id"] = str(sales_wechat_id).strip()
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=10.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json()
                return {"code": resp.status_code, "message": "请求失败", "data": [], "has_more": False}
        except Exception as e:
            logger.warning(f"拉取微信原始聊天记录异常 (ID: {raw_customer_id}): {e}")
            return {"code": 500, "message": str(e), "data": [], "has_more": False}

    async def set_message_feedback(self, msg_id: int, rating: int):
        """提交对某条 AI 回复的消息评价 (1:赞, -1:踩)"""
        if not self.token: return None
        url = f"{self.base_url}/api/customer/message/{msg_id}/feedback"
        headers = {"Authorization": f"Bearer {self.token}"}
        params = {"rating": rating}
        try:
            async with _dummy_client(self.client, timeout=5.0) as client:
                resp = await client.post(url, headers=headers, params=params)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"提交消息评价异常: {e}")
            return None

    async def record_message_copy(self, msg_id: int):
        """记录该条 AI 回复被用户复制的采纳行为"""
        if not self.token: return None
        url = f"{self.base_url}/api/customer/message/{msg_id}/copy"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=5.0) as client:
                resp = await client.post(url, headers=headers)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"记录消息复制行为异常: {e}")
            return None

    async def register_account(
        self,
        username: str,
        password: str,
        real_name: str,
        sales_wechat_ids: list,
    ):
        """自助注册（无需 token）。"""
        url = f"{self.base_url}/api/auth/register"
        payload = {
            "username": username,
            "password": password,
            "real_name": real_name,
            "sales_wechat_ids": sales_wechat_ids,
        }
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    return True, resp.json().get("message", "注册成功")
                try:
                    payload = resp.json()
                    detail = payload.get("detail") or payload.get("message") or resp.text
                except Exception:
                    detail = resp.text or "注册失败"
                return False, str(detail)
        except Exception as e:
            return False, f"无法连接服务器: {e}"

    async def list_sales_wechats(self):
        if not self.token:
            return None
        url = f"{self.base_url}/api/me/sales-wechats"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.get(url, headers=headers)
                self._check_auth(resp)
                if resp.status_code == 200:
                    body = resp.json()
                    return body.get("data", []) if isinstance(body, dict) else body
                return None
        except Exception as e:
            logger.warning(f"拉取销售微信号绑定异常: {e}")
            return None

    async def add_sales_wechat_bind(self, sales_wechat_id: str, label: str = None, is_primary: bool = False):
        if not self.token:
            return None
        url = f"{self.base_url}/api/me/sales-wechats"
        headers = {"Authorization": f"Bearer {self.token}"}
        body = {"sales_wechat_id": sales_wechat_id, "is_primary": is_primary}
        if label:
            body["label"] = label
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, json=body, headers=headers)
                self._check_auth(resp)
                try:
                    data = resp.json()
                except Exception:
                    data = {"message": resp.text}
                if resp.status_code == 200:
                    return data
                return data
        except Exception as e:
            logger.warning(f"添加销售微信号异常: {e}")
            return None

    async def delete_sales_wechat_bind(self, binding_id: int):
        if not self.token:
            return None
        url = f"{self.base_url}/api/me/sales-wechats/{binding_id}"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.delete(url, headers=headers)
                self._check_auth(resp)
                return resp.status_code in (200, 204)
        except Exception as e:
            logger.warning(f"删除销售微信号绑定异常: {e}")
            return False

    async def set_primary_sales_wechat_bind(self, binding_id: int):
        if not self.token:
            return None
        url = f"{self.base_url}/api/me/sales-wechats/{binding_id}/set-primary"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, headers=headers)
                self._check_auth(resp)
                return resp.json() if resp.status_code == 200 else None
        except Exception as e:
            logger.warning(f"设主号异常: {e}")
            return None

    async def get_mibuddy_binding(self):
        if not self.token:
            return None
        url = f"{self.base_url}/api/me/mibuddy"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.get(url, headers=headers)
                self._check_auth(resp)
                if resp.status_code == 200:
                    body = resp.json()
                    return body.get("data") if isinstance(body, dict) else None
                return None
        except Exception as e:
            logger.warning(f"拉取米城 UUID 绑定异常: {e}")
            return None

    async def bind_mibuddy_uuid(self, uuid: str):
        if not self.token:
            return None
        url = f"{self.base_url}/api/me/mibuddy"
        headers = {"Authorization": f"Bearer {self.token}"}
        body = {"uuid": (uuid or "").strip()}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, json=body, headers=headers)
                self._check_auth(resp)
                try:
                    data = resp.json()
                except Exception:
                    data = {"message": resp.text}
                if resp.status_code == 200:
                    return data
                return data
        except Exception as e:
            logger.warning(f"绑定米城 UUID 异常: {e}")
            return None

    async def unbind_mibuddy_uuid(self):
        if not self.token:
            return False
        url = f"{self.base_url}/api/me/mibuddy"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.delete(url, headers=headers)
                self._check_auth(resp)
                return resp.status_code in (200, 204)
        except Exception as e:
            logger.warning(f"解绑米城 UUID 异常: {e}")
            return False

    async def get_mibuddy_claimed_leads(
        self,
        page: int = 1,
        page_size: int = 50,
        *,
        sort: str = "assign_time",
        order: str = "asc",
    ):
        if not self.token:
            return None
        url = f"{self.base_url}/api/me/mibuddy/my-leads"
        headers = {"Authorization": f"Bearer {self.token}"}
        sort_field = (sort or "assign_time").strip()
        if sort_field not in ("assign_time", "operate_time"):
            sort_field = "assign_time"
        order_dir = (order or "asc").strip().lower()
        if order_dir not in ("asc", "desc"):
            order_dir = "asc"
        params = {
            "page": max(1, int(page or 1)),
            "page_size": max(1, int(page_size or 50)),
            "sort": sort_field,
            "order": order_dir,
        }
        # 上游 my_leads 偏慢（约 3s/20），单独放宽超时，避免大页/拥堵误杀
        leads_timeout = max(float(cfg.timeout), 30.0)
        try:
            async with _dummy_client(self.client, timeout=leads_timeout) as client:
                resp = await client.get(url, headers=headers, params=params)
                self._check_auth(resp)
                if resp.status_code == 200:
                    body = resp.json()
                    return body if isinstance(body, dict) else None
                try:
                    data = resp.json()
                except Exception:
                    data = {"message": resp.text}
                return data
        except Exception as e:
            logger.warning(f"拉取认领客资异常: {type(e).__name__}: {e!r}")
            return None

    async def get_mibuddy_favorite_leads(
        self,
        page: int = 1,
        page_size: int = 50,
        client_name: str | None = None,
        *,
        sort: str = "collected_time",
        order: str = "desc",
        tag: str | None = None,
        color: str | None = None,
        province: str | None = None,
        city: str | None = None,
        county: str | None = None,
        buy_month: int | None = None,
        buyer_type: int | None = None,
    ):
        if not self.token:
            return None
        url = f"{self.base_url}/api/me/mibuddy/my-leads-album"
        headers = {"Authorization": f"Bearer {self.token}"}
        sort_field = (sort or "collected_time").strip()
        if sort_field not in ("collected_time", "operate_time"):
            sort_field = "collected_time"
        order_dir = (order or "desc").strip().lower()
        if order_dir not in ("asc", "desc"):
            order_dir = "desc"
        params: dict = {
            "page": max(1, int(page or 1)),
            "page_size": max(1, int(page_size or 50)),
            "sort": sort_field,
            "order": order_dir,
        }
        keyword = (client_name or "").strip()
        if keyword:
            params["client_name"] = keyword
        tag_code = (tag or "").strip()
        if tag_code:
            params["tag"] = tag_code
        color_code = (color or "").strip().lower()
        if color_code:
            params["color"] = color_code
        for key, value in (
            ("province", province),
            ("city", city),
            ("county", county),
        ):
            text = (value or "").strip()
            if text:
                params[key] = text
        if buy_month is not None:
            try:
                month = int(buy_month)
            except (TypeError, ValueError):
                month = 0
            if 1 <= month <= 12:
                params["buy_month"] = month
        if buyer_type is not None:
            try:
                btype = int(buyer_type)
            except (TypeError, ValueError):
                btype = 0
            if btype in (1, 2, 3, 4):
                params["buyer_type"] = btype
        leads_timeout = max(float(cfg.timeout), 30.0)
        try:
            async with _dummy_client(self.client, timeout=leads_timeout) as client:
                resp = await client.get(url, headers=headers, params=params)
                self._check_auth(resp)
                if resp.status_code == 200:
                    body = resp.json()
                    return body if isinstance(body, dict) else None
                try:
                    data = resp.json()
                except Exception:
                    data = {"message": resp.text}
                return data
        except Exception as e:
            logger.warning(f"拉取收藏客资异常: {type(e).__name__}: {e!r}")
            return None

    async def call_mibuddy_changhu(
        self,
        *,
        changhu_tel: str,
        tel: str | None = None,
        lead_id: int | None = None,
        user_wechat_account: str | None = None,
    ):
        if not self.token:
            return None
        caller = (changhu_tel or "").strip()
        if not caller:
            return None
        url = f"{self.base_url}/api/me/mibuddy/call-changhu"
        headers = {"Authorization": f"Bearer {self.token}"}
        body: dict = {"changhu_tel": caller}
        phone = (tel or "").strip()
        if phone:
            body["tel"] = phone
        if lead_id is not None:
            try:
                body["lead_id"] = int(lead_id)
            except (TypeError, ValueError):
                pass
        account = (user_wechat_account or "").strip()
        if account:
            body["user_wechat_account"] = account
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
                self._check_auth(resp)
                try:
                    data = resp.json()
                except Exception:
                    data = {"message": resp.text}
                if resp.status_code == 200:
                    return data
                return data
        except Exception as e:
            logger.warning(f"畅呼外呼异常: {type(e).__name__}: {e!r}")
            return None

    async def call_mibuddy_yunke(
        self,
        *,
        tel: str | None = None,
        lead_id: int | None = None,
        user_wechat_account: str | None = None,
    ):
        if not self.token:
            return None
        url = f"{self.base_url}/api/me/mibuddy/call-yunke"
        headers = {"Authorization": f"Bearer {self.token}"}
        body: dict = {}
        phone = (tel or "").strip()
        if phone:
            body["tel"] = phone
        if lead_id is not None:
            try:
                body["lead_id"] = int(lead_id)
            except (TypeError, ValueError):
                pass
        account = (user_wechat_account or "").strip()
        if account:
            body["user_wechat_account"] = account
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
                self._check_auth(resp)
                try:
                    data = resp.json()
                except Exception:
                    data = {"message": resp.text}
                if resp.status_code == 200:
                    return data
                return data
        except Exception as e:
            logger.warning(f"云客外呼异常: {type(e).__name__}: {e!r}")
            return None

    async def approve_mibuddy_lead_tel(self, lead_id: int):
        if not self.token:
            return None
        url = f"{self.base_url}/api/me/mibuddy/leads/{int(lead_id)}/approval_tel"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, headers=headers)
                self._check_auth(resp)
                try:
                    data = resp.json()
                except Exception:
                    data = {"message": resp.text}
                if resp.status_code == 200:
                    return data
                return data
        except Exception as e:
            logger.warning(f"申请查看电话异常: {type(e).__name__}: {e!r}")
            return None

    async def ignore_mibuddy_lead(self, lead_id: int):
        if not self.token:
            return None
        url = f"{self.base_url}/api/me/mibuddy/leads/{int(lead_id)}/ignore"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, headers=headers)
                self._check_auth(resp)
                try:
                    data = resp.json()
                except Exception:
                    data = {"message": resp.text}
                if resp.status_code == 200:
                    return data
                return data
        except Exception as e:
            logger.warning(f"移除客资异常: {type(e).__name__}: {e!r}")
            return None

    async def add_mibuddy_lead_remark(self, lead_id: int, remark: str):
        if not self.token:
            return None
        url = f"{self.base_url}/api/me/mibuddy/leads/{int(lead_id)}/remarks"
        headers = {"Authorization": f"Bearer {self.token}"}
        body = {"remark": (remark or "").strip()}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, json=body, headers=headers)
                self._check_auth(resp)
                try:
                    data = resp.json()
                except Exception:
                    data = {"message": resp.text}
                if resp.status_code == 200:
                    return data
                return data
        except Exception as e:
            logger.warning(f"提交客资跟进备注异常: {type(e).__name__}: {e!r}")
            return None

    async def get_mibuddy_lead_remarks(
        self, lead_id: int, page: int = 1, page_size: int = 20
    ):
        if not self.token:
            return None
        url = f"{self.base_url}/api/me/mibuddy/leads/{int(lead_id)}/remarks"
        headers = {"Authorization": f"Bearer {self.token}"}
        params = {"page": max(1, int(page or 1)), "page_size": max(1, int(page_size or 20))}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.get(url, headers=headers, params=params)
                self._check_auth(resp)
                if resp.status_code == 200:
                    body = resp.json()
                    return body if isinstance(body, dict) else None
                try:
                    data = resp.json()
                except Exception:
                    data = {"message": resp.text}
                return data
        except Exception as e:
            logger.warning(f"拉取客资跟进备注异常: {type(e).__name__}: {e!r}")
            return None

    async def update_mibuddy_lead(self, lead_id: int, info: dict):
        if not self.token:
            return None
        url = f"{self.base_url}/api/me/mibuddy/leads/{int(lead_id)}"
        headers = {"Authorization": f"Bearer {self.token}"}
        body = {"info": info or {}}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.patch(url, json=body, headers=headers)
                self._check_auth(resp)
                try:
                    data = resp.json()
                except Exception:
                    data = {"message": resp.text}
                if resp.status_code == 200:
                    return data
                return data
        except Exception as e:
            logger.warning(f"更新客资异常: {type(e).__name__}: {e!r}")
            return None

    async def get_tasks_overview(
        self,
        period: str = "daily",
        sales_wechat_id: Optional[str] = None,
        date_str: Optional[str] = None,
        status: Optional[str] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ):
        """拉取任务分配总览（按销售微信 + 周期）。"""
        if not self.token:
            return None
        url = f"{self.base_url}/api/tasks/overview"
        headers = {"Authorization": f"Bearer {self.token}"}
        params: dict = {"period": period}
        if sales_wechat_id:
            params["sales_wechat_id"] = str(sales_wechat_id).strip()
        if date_str:
            params["date"] = date_str
        if status:
            params["status"] = status
        if page is not None:
            params["page"] = int(page)
        if page_size is not None:
            params["page_size"] = int(page_size)
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.get(url, params=params, headers=headers)
                self._check_auth(resp)
                try:
                    return resp.json()
                except Exception:
                    return {"code": resp.status_code, "message": resp.text, "data": None}
        except Exception as e:
            logger.warning(f"拉取任务分配总览异常: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def claim_more_tasks(
        self,
        sales_wechat_id: Optional[str] = None,
        count: int = 5,
    ):
        """从储备池批量认领任务（默认一次 5 条）。"""
        if not self.token:
            return None
        url = f"{self.base_url}/api/tasks/claim-more"
        headers = {"Authorization": f"Bearer {self.token}"}
        params: dict = {"count": max(1, min(int(count or 5), 5))}
        if sales_wechat_id:
            params["sales_wechat_id"] = str(sales_wechat_id).strip()
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, params=params, headers=headers)
                self._check_auth(resp)
                try:
                    data = resp.json()
                except Exception:
                    return {"code": resp.status_code, "message": resp.text, "data": None}
                if isinstance(data, dict) and "code" not in data:
                    detail = data.get("detail") or data.get("message") or resp.text
                    return {"code": resp.status_code, "message": detail, "data": None}
                return data
        except Exception as e:
            logger.warning(f"批量认领任务异常: {e}")
            return {"code": 500, "message": str(e), "data": None}

    @staticmethod
    def _normalize_task_action_resp(resp) -> dict:
        """统一任务操作响应；FastAPI 400 常为 {detail}，转为 {code, message}。"""
        try:
            data = resp.json()
        except Exception:
            return {"code": resp.status_code, "message": resp.text, "data": None}
        if not isinstance(data, dict):
            return {"code": resp.status_code, "message": str(data), "data": None}
        if "code" not in data:
            detail = data.get("detail") or data.get("message") or resp.text
            return {"code": resp.status_code, "message": detail, "data": None}
        return data

    async def complete_task(
        self,
        task_id: int,
        note: Optional[str] = None,
        sales_wechat_id: Optional[str] = None,
    ):
        """标记联系任务为已完成。"""
        if not self.token:
            return None
        url = f"{self.base_url}/api/tasks/{int(task_id)}/complete"
        headers = {"Authorization": f"Bearer {self.token}"}
        payload = {"note": note} if note else {}
        params: dict = {}
        sw = str(sales_wechat_id or "").strip()
        if sw:
            params["sales_wechat_id"] = sw
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers, params=params or None)
                self._check_auth(resp)
                return self._normalize_task_action_resp(resp)
        except Exception as e:
            logger.warning(f"完成任务异常 task_id={task_id}: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def skip_task(
        self,
        task_id: int,
        note: Optional[str] = None,
        sales_wechat_id: Optional[str] = None,
    ):
        """标记联系任务为已跳过。"""
        if not self.token:
            return None
        url = f"{self.base_url}/api/tasks/{int(task_id)}/skip"
        headers = {"Authorization": f"Bearer {self.token}"}
        payload = {"note": note} if note else {}
        params: dict = {}
        sw = str(sales_wechat_id or "").strip()
        if sw:
            params["sales_wechat_id"] = sw
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers, params=params or None)
                self._check_auth(resp)
                return self._normalize_task_action_resp(resp)
        except Exception as e:
            logger.warning(f"跳过任务异常 task_id={task_id}: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def appeal_task(
        self,
        task_id: int,
        reason: str,
        sales_wechat_id: Optional[str] = None,
    ):
        """申诉任务（采集原因用于优化分配）。"""
        if not self.token:
            return None
        url = f"{self.base_url}/api/tasks/{int(task_id)}/appeal"
        headers = {"Authorization": f"Bearer {self.token}"}
        payload = {"reason": str(reason or "").strip()}
        params: dict = {}
        sw = str(sales_wechat_id or "").strip()
        if sw:
            params["sales_wechat_id"] = sw
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers, params=params or None)
                self._check_auth(resp)
                return self._normalize_task_action_resp(resp)
        except Exception as e:
            logger.warning(f"申诉任务异常 task_id={task_id}: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def restore_task(
        self,
        task_id: int,
        sales_wechat_id: Optional[str] = None,
    ):
        """将已完成 / 已跳过的任务恢复为待办。"""
        if not self.token:
            return None
        url = f"{self.base_url}/api/tasks/{int(task_id)}/restore"
        headers = {"Authorization": f"Bearer {self.token}"}
        params: dict = {}
        sw = str(sales_wechat_id or "").strip()
        if sw:
            params["sales_wechat_id"] = sw
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, headers=headers, params=params or None)
                self._check_auth(resp)
                return self._normalize_task_action_resp(resp)
        except Exception as e:
            logger.warning(f"恢复任务异常 task_id={task_id}: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def create_wechat_outbound_action(self, payload: dict):
        """创建「发微信」审计记录，返回 data 含 id、receiver 等。"""
        if not self.token:
            return None
        url = f"{self.base_url}/api/wechat/outbound-actions"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                self._check_auth(resp)
                try:
                    return resp.json()
                except Exception:
                    return {"code": resp.status_code, "message": resp.text, "data": None}
        except Exception as e:
            logger.warning(f"创建微信外发审计失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def list_wechat_outbound_actions(
        self,
        *,
        raw_customer_id: str | None = None,
        status: str = "sent,failed",
        limit: int = 20,
    ):
        """拉取本人历史外发正文（编辑弹窗复用改稿）。"""
        if not self.token:
            return None
        url = f"{self.base_url}/api/wechat/outbound-actions"
        headers = {"Authorization": f"Bearer {self.token}"}
        params: dict = {
            "status": (status or "sent,failed").strip(),
            "limit": max(1, min(50, int(limit or 20))),
        }
        cid = (raw_customer_id or "").strip()
        if cid:
            params["raw_customer_id"] = cid
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.get(url, headers=headers, params=params)
                self._check_auth(resp)
                try:
                    return resp.json()
                except Exception:
                    return {"code": resp.status_code, "message": resp.text, "data": None}
        except Exception as e:
            logger.warning(f"拉取微信外发历史失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def list_active_campaigns(
        self,
        raw_customer_id: str,
        sales_wechat_id: Optional[str] = None,
    ):
        """当前客户匹配的进行中活动及下一张轮询海报。"""
        if not self.token:
            return None
        url = f"{self.base_url}/api/campaigns/active"
        headers = {"Authorization": f"Bearer {self.token}"}
        params: dict = {"raw_customer_id": (raw_customer_id or "").strip()}
        sw = str(sales_wechat_id or "").strip()
        if sw:
            params["sales_wechat_id"] = sw
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.get(url, headers=headers, params=params)
                self._check_auth(resp)
                try:
                    return resp.json()
                except Exception:
                    return {"code": resp.status_code, "message": resp.text, "data": None}
        except Exception as e:
            logger.warning(f"拉取匹配活动失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    def _media_cache_path(self, image_path: str) -> str | None:
        """活动海报明文文件缓存路径（桌面端需原图给 RPA，不走 SQLite 加密 blob）。"""
        if not self.storage:
            return None
        rel = (image_path or "").strip()
        if not rel:
            return None
        key = self._generate_cache_key("campaign_poster", path=rel)
        raw_path = urlparse(rel).path if rel.startswith(("http://", "https://")) else rel
        suffix = os.path.splitext(raw_path)[1].lower() or ".jpg"
        if len(suffix) > 8 or not suffix.startswith(".") or not suffix[1:].isalnum():
            suffix = ".jpg"
        posters_dir = os.path.join(self.storage.user_dir, "campaign_posters")
        os.makedirs(posters_dir, exist_ok=True)
        return os.path.join(posters_dir, f"{key}{suffix}")

    def get_cached_media_path(self, image_path: str) -> str | None:
        """命中本地海报缓存则返回路径，否则 None。"""
        path = self._media_cache_path(image_path)
        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        return None

    async def ensure_media_local(self, image_path: str) -> str | None:
        """确保海报在本地可读：优先磁盘缓存，未命中则下载并写入缓存。"""
        rel = (image_path or "").strip()
        if not rel:
            return None
        cached = self.get_cached_media_path(rel)
        if cached:
            return cached

        inflight = self._media_inflight.get(rel)
        if inflight is not None and not inflight.done():
            try:
                return await inflight
            except Exception:
                return self.get_cached_media_path(rel)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._media_inflight[rel] = fut
        try:
            target = self._media_cache_path(rel)
            if not target:
                fut.set_result(None)
                return None
            ok = await self.download_media(rel, target)
            if ok and os.path.isfile(target) and os.path.getsize(target) > 0:
                fut.set_result(target)
                return target
            try:
                if os.path.exists(target):
                    os.remove(target)
            except OSError:
                pass
            fut.set_result(None)
            return None
        except Exception as e:
            if not fut.done():
                fut.set_result(None)
            logger.warning(f"缓存海报失败: {e}")
            return None
        finally:
            if self._media_inflight.get(rel) is fut:
                self._media_inflight.pop(rel, None)

    async def download_media(self, image_path: str, target_path: str) -> bool:
        """下载 /media 或绝对 URL 到本地文件，供海报预览与 RPA 粘贴。"""
        rel = (image_path or "").strip()
        if not rel:
            return False
        if rel.startswith("http://") or rel.startswith("https://"):
            url = rel
        else:
            if not rel.startswith("/"):
                rel = "/" + rel
            url = f"{self.base_url}{rel}"
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with _dummy_client(self.client, timeout=30.0) as client:
                resp = await client.get(url, headers=headers or None)
                if resp.status_code != 200 or not resp.content:
                    logger.warning(f"下载海报失败 status={resp.status_code} url={url}")
                    return False
                parent = os.path.dirname(target_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                # 先写临时文件再替换，避免半截缓存被当成命中
                tmp_path = f"{target_path}.part"
                with open(tmp_path, "wb") as output:
                    output.write(resp.content)
                os.replace(tmp_path, target_path)
                return True
        except Exception as e:
            logger.warning(f"下载海报异常: {e}")
            try:
                part = f"{target_path}.part"
                if os.path.exists(part):
                    os.remove(part)
            except OSError:
                pass
            return False

    async def report_wechat_outbound_result(self, action_id: int, payload: dict):
        """回写 RPA 执行结果。"""
        if not self.token:
            return None
        url = f"{self.base_url}/api/wechat/outbound-actions/{action_id}/result"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                self._check_auth(resp)
                try:
                    return resp.json()
                except Exception:
                    return {"code": resp.status_code, "message": resp.text, "data": None}
        except Exception as e:
            logger.warning(f"回写微信外发结果失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def get_callbacks(self, sales_wechat_id: Optional[str] = None):
        """拉取再联系提醒列表（当日 + 往日逾期）。"""
        if not self.token:
            return None
        url = f"{self.base_url}/api/tasks/callbacks"
        headers = {"Authorization": f"Bearer {self.token}"}
        params: dict = {}
        if sales_wechat_id:
            params["sales_wechat_id"] = str(sales_wechat_id).strip()
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.get(url, params=params or None, headers=headers)
                self._check_auth(resp)
                try:
                    return resp.json()
                except Exception:
                    return {"code": resp.status_code, "message": resp.text, "data": None}
        except Exception as e:
            logger.warning(f"拉取回访提醒异常: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def mark_callback_done(
        self,
        scp_id: int,
        sales_wechat_id: Optional[str] = None,
    ):
        """标记回访已处理。"""
        if not self.token:
            return None
        url = f"{self.base_url}/api/tasks/callbacks/{int(scp_id)}/done"
        headers = {"Authorization": f"Bearer {self.token}"}
        params: dict = {}
        if sales_wechat_id:
            params["sales_wechat_id"] = str(sales_wechat_id).strip()
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, params=params or None, headers=headers)
                self._check_auth(resp)
                try:
                    return resp.json()
                except Exception:
                    return {"code": resp.status_code, "message": resp.text, "data": None}
        except Exception as e:
            logger.warning(f"标记回访已处理异常: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def list_running_campaigns(self, unit_type: str):
        if not self.token:
            return None
        url = f"{self.base_url}/api/campaigns/running"
        headers = {"Authorization": f"Bearer {self.token}"}
        params = {"unit_type": (unit_type or "").strip()}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.get(url, headers=headers, params=params)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"拉取进行中活动失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def create_campaign_blast_job(self, payload: dict):
        if not self.token:
            return None
        url = f"{self.base_url}/api/campaigns/blast/jobs"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"创建活动群发任务失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def get_current_campaign_blast_job(
        self,
        sales_wechat_id: str,
        campaign_id: int | None = None,
        *,
        job_kind: str = "campaign",
        unit_type: str | None = None,
    ):
        if not self.token:
            return None
        url = f"{self.base_url}/api/campaigns/blast/jobs/current"
        headers = {"Authorization": f"Bearer {self.token}"}
        params: dict = {
            "sales_wechat_id": (sales_wechat_id or "").strip(),
            "job_kind": (job_kind or "campaign").strip() or "campaign",
        }
        if campaign_id is not None and int(campaign_id) > 0:
            params["campaign_id"] = int(campaign_id)
        if unit_type:
            params["unit_type"] = unit_type.strip()
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.get(url, headers=headers, params=params)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"拉取当前群发任务失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def get_campaign_blast_job(self, job_id: int):
        if not self.token:
            return None
        url = f"{self.base_url}/api/campaigns/blast/jobs/{int(job_id)}"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.get(url, headers=headers)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"拉取群发任务失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def search_campaign_blast_candidates(
        self,
        *,
        sales_wechat_id: str,
        campaign_id: int | None = None,
        job_kind: str = "campaign",
        job_id: int | None = None,
        unit_type: str | None = None,
        q: str = "",
        skip: int = 0,
        limit: int = 50,
    ):
        if not self.token:
            return None
        url = f"{self.base_url}/api/campaigns/blast/candidates"
        headers = {"Authorization": f"Bearer {self.token}"}
        params: dict = {
            "sales_wechat_id": (sales_wechat_id or "").strip(),
            "job_kind": (job_kind or "campaign").strip() or "campaign",
            "skip": int(skip),
            "limit": int(limit),
        }
        if campaign_id is not None and int(campaign_id) > 0:
            params["campaign_id"] = int(campaign_id)
        if job_id:
            params["job_id"] = int(job_id)
        if unit_type:
            params["unit_type"] = unit_type.strip()
        if q:
            params["q"] = q.strip()
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.get(url, headers=headers, params=params)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"检索群发候选失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def upload_campaign_blast_custom_image(
        self, job_id: int, file_path: str, *, media_mode: str | None = None
    ):
        if not self.token:
            return None
        url = f"{self.base_url}/api/campaigns/blast/jobs/{int(job_id)}/custom-image"
        headers = {"Authorization": f"Bearer {self.token}"}
        path = (file_path or "").strip()
        if not path:
            return {"code": 400, "message": "未选择文件", "data": None}
        try:
            from pathlib import Path

            name = Path(path).name or "custom.png"
            with open(path, "rb") as f:
                content = f.read()
            files = {"file": (name, content)}
            data = {}
            if media_mode:
                data["media_mode"] = str(media_mode).strip()
            async with _dummy_client(self.client, timeout=max(60, cfg.timeout)) as client:
                resp = await client.post(url, headers=headers, files=files, data=data or None)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"上传自定义群发图片失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def delete_campaign_blast_custom_image(self, job_id: int):
        if not self.token:
            return None
        url = f"{self.base_url}/api/campaigns/blast/jobs/{int(job_id)}/custom-image"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.delete(url, headers=headers)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"清除自定义群发图片失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def patch_campaign_blast_custom_meta(
        self, job_id: int, *, custom_brief: str | None = None, media_mode: str | None = None
    ):
        if not self.token:
            return None
        url = f"{self.base_url}/api/campaigns/blast/jobs/{int(job_id)}/custom-meta"
        headers = {"Authorization": f"Bearer {self.token}"}
        body: dict = {}
        if custom_brief is not None:
            body["custom_brief"] = custom_brief
        if media_mode is not None:
            body["media_mode"] = media_mode
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.patch(url, json=body, headers=headers)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"更新自定义群发元数据失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def add_campaign_blast_recipients(self, job_id: int, raw_customer_ids: list[str]):
        if not self.token:
            return None
        url = f"{self.base_url}/api/campaigns/blast/jobs/{int(job_id)}/recipients"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(
                    url,
                    json={"raw_customer_ids": raw_customer_ids},
                    headers=headers,
                )
                return self._parse_json(resp)
        except Exception as e:
            logger.warning(f"添加群发名单失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def delete_campaign_blast_recipients(self, job_id: int, recipient_ids: list[int]):
        if not self.token:
            return None
        url = f"{self.base_url}/api/campaigns/blast/jobs/{int(job_id)}/recipients/delete"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(
                    url,
                    json={"recipient_ids": recipient_ids},
                    headers=headers,
                )
                return self._parse_json(resp)
        except Exception as e:
            logger.warning(f"删除群发名单失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def generate_campaign_blast_scripts(
        self, job_id: int, recipient_ids: list[int] | None = None
    ):
        if not self.token:
            return None
        url = f"{self.base_url}/api/campaigns/blast/jobs/{int(job_id)}/generate-scripts"
        headers = {"Authorization": f"Bearer {self.token}"}
        body = {}
        if recipient_ids:
            body["recipient_ids"] = recipient_ids
        try:
            # 单批约 8 人、1 次 LLM；与后端 llm_client HTTP_TIMEOUT(300s) 对齐并留余量
            async with _dummy_client(self.client, timeout=max(330, cfg.timeout * 12)) as client:
                resp = await client.post(url, json=body, headers=headers)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"生成群发话术失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def patch_campaign_blast_script(
        self, job_id: int, recipient_id: int, script_text: str
    ):
        if not self.token:
            return None
        url = (
            f"{self.base_url}/api/campaigns/blast/jobs/{int(job_id)}"
            f"/recipients/{int(recipient_id)}"
        )
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.patch(
                    url,
                    json={"script_text": script_text},
                    headers=headers,
                )
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"保存群发话术失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def retry_campaign_blast_failed(self, job_id: int):
        if not self.token:
            return None
        url = f"{self.base_url}/api/campaigns/blast/jobs/{int(job_id)}/retry-failed"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, headers=headers)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"重试群发失败项失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def start_campaign_blast_sending(self, job_id: int):
        if not self.token:
            return None
        url = f"{self.base_url}/api/campaigns/blast/jobs/{int(job_id)}/start-sending"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, headers=headers)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"启动群发发送失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    async def ack_campaign_blast_recipient(
        self,
        job_id: int,
        recipient_id: int,
        *,
        success: bool,
        outbound_action_id: int | None = None,
        error_message: str | None = None,
    ):
        if not self.token:
            return None
        url = (
            f"{self.base_url}/api/campaigns/blast/jobs/{int(job_id)}"
            f"/recipients/{int(recipient_id)}/ack"
        )
        headers = {"Authorization": f"Bearer {self.token}"}
        body = {
            "success": bool(success),
            "outbound_action_id": outbound_action_id,
            "error_message": error_message,
        }
        try:
            async with _dummy_client(self.client, timeout=cfg.timeout) as client:
                resp = await client.post(url, json=body, headers=headers)
                self._check_auth(resp)
                return resp.json()
        except Exception as e:
            logger.warning(f"群发回执失败: {e}")
            return {"code": 500, "message": str(e), "data": None}

    def logout(self):
        """彻底销毁内存令牌，解除存储挂载"""
        if self.storage is not None:
            try:
                self.storage.close()
            except Exception:
                pass
        self.token = None
        self.user_data = None
        self.storage = None
