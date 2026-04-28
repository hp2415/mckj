import os
import json
import httpx
import hashlib
import contextlib
from typing import Optional
from urllib.parse import quote
from PySide6.QtCore import QObject, Signal

@contextlib.asynccontextmanager
async def _dummy_client(client, timeout=None):
    if timeout:
        client.timeout = httpx.Timeout(timeout)
    yield client

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
        
        try:
            async with _dummy_client(self.client, timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                self._check_auth(resp)
                if resp.status_code == 200:
                    return resp.json()
                return None
        except Exception as e:
            logger.warning(f"拉取客户列表异常: {e}")
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
        sales_wechat_id: Optional[str] = None,
        scenario: str = "general_chat",
        conversation_id: str = None,
        chat_model: str = None,
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
        if sales_wechat_id:
            payload["sales_wechat_id"] = str(sales_wechat_id).strip()
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if chat_model:
            payload["chat_model"] = chat_model

        async with _dummy_client(self.client, timeout=90.0) as client:
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
                            }
                            yield f"[META_MODEL:{json.dumps(meta, ensure_ascii=False)}]"
                        elif event == "done":
                            msg_id = data.get("msg_id")
                            if msg_id:
                                yield f"[MSG_ID:{msg_id}]"
                        elif event == "system_action":
                            changes = data.get("changes", {})
                            yield f"[SYSTEM_ACTION:{json.dumps(changes, ensure_ascii=False)}]"
                        elif event == "error":
                            yield f"Error: {data.get('text', '未知错误')}"
                    except (json.JSONDecodeError, KeyError):
                        continue

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

    async def get_chat_history(self, phone: str, limit: int = 20, skip: int = 0):
        """获取后端存储历史 AI 聊天记录（旧：按手机号）。"""
        if not self.token: return []
        url = f"{self.base_url}/api/customer/{phone}/chat_history"
        params = {"limit": limit, "skip": skip}
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

    async def get_chat_history_by_id(self, raw_customer_id: str, limit: int = 20, skip: int = 0):
        """按 raw_customer_id 拉取历史聊天记录（推荐：不依赖手机号）。"""
        if not self.token:
            return []
        seg = quote(str(raw_customer_id), safe="")
        url = f"{self.base_url}/api/customer/id/{seg}/chat_history"
        params = {"limit": limit, "skip": skip}
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
                    detail = resp.json().get("detail", resp.text)
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

    def logout(self):
        """彻底销毁内存令牌，解除存储挂载"""
        self.token = None
        self.user_data = None
        self.storage = None
