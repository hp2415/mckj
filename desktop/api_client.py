import httpx
import hashlib
import json
from storage import SecureStorage

class APIClient:
    """
    桌面端核心通讯器。
    集成了：JWT 内存化管理、基于用户隔离的本地加密缓存、以及自动重连/重试机制。
    """
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url.rstrip("/")
        self.token = None          # 令牌始终仅在内存中持有，不落盘
        self.user_data = None      # 存放当前登录用户的元数据
        self.storage = None        # 根据登录用户动态加载的加密存储库
        
    def _generate_cache_key(self, endpoint: str, **params) -> str:
        """根据路径和参数生成唯一的哈希键，防止文件名非法字符"""
        query_str = json.dumps(params, sort_keys=True)
        return hashlib.sha256(f"{endpoint}_{query_str}".encode()).hexdigest()

    async def login(self, username, password):
        """对接 FastAPI 后端登录逻辑"""
        url = f"{self.base_url}/api/auth/login"
        payload = {"username": username, "password": password}
        try:
            # 采用 x-www-form-urlencoded 格式发送登录请求
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, data=payload)
                if response.status_code == 200:
                    data = response.json()
                    self.token = data["access_token"]
                    self.user_data = data
                    # 登录成功后，立即激活基于 user_id 的加密存储
                    self.storage = SecureStorage(data["user_id"])
                    return True, "登录成功"
                else:
                    detail = response.json().get("detail", "账号或密码错误")
                    return False, detail
        except Exception as e:
            return False, f"无法连接到服务器: {str(e)}"

    async def search_products(self, keyword: str = "", skip: int = 0, limit: int = 20):
        """优先读取本地缓存，本地无数据则请求后端并回流缓存"""
        if not self.storage:
            return None
            
        cache_key = self._generate_cache_key("product_search", k=keyword, s=skip, l=limit)
        
        # 1. 尝试从本地强加密缓存中读取 (减少后端并发压力)
        cached_data = self.storage.load_json(cache_key)
        if cached_data:
            return cached_data
            
        # 2. 本地不存在，则向后端请求
        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.base_url}/api/product/search"
        params = {"keyword": keyword, "skip": skip, "limit": limit}
        
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    # 3. 静默回流缓存，方便下次秒开
                    self.storage.save_json(cache_key, data)
                    return data
                return None
        except Exception:
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
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return resp.json()
                return None
        except Exception:
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
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.patch(url, params=params, json=update_data, headers=headers)
                return resp.json()
        except Exception as e:
            return {"code": 500, "message": str(e)}

    def logout(self):
        """彻底销毁内存令牌，解除存储挂载"""
        self.token = None
        self.user_data = None
        self.storage = None
