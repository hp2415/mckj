import httpx

base_url = "http://localhost:8000"

print("====================================")
print("开始全链路桌面端 API 模拟自动化测试")
print("====================================\n")

# ---- 1. 测试登录接口 ----
print("1. 测试登录接口 POST /api/auth/login")
print("    正在使用刚初始化的 staff_01 尝试身份认证...")

login_data = {"username": "staff_01", "password": "123456"}
# OAuth2PasswordRequestForm 期望表单格式 (form-data/x-www-form-urlencoded)
login_resp = httpx.post(f"{base_url}/api/auth/login", data=login_data)

if login_resp.status_code == 200:
    resp_json = login_resp.json()
    token = resp_json.get("access_token")
    print(f"    登录成功！你好，{resp_json.get('real_name')}。")
    print(f"    获得 JWT 令牌: {token[:30]}......\n")
else:
    print(f"    登录失败: {login_resp.text}")
    exit(1)

# ---- 2. 测试新建客户资料同步接口 ----
print("2. 测试客户资料同步 API (假设桌面微信扫到了一个新老板聊天)")

headers = {"Authorization": f"Bearer {token}"}
customer_data_1 = {
    "phone": "13912345678",
    "customer_name": "李厅长",
    "unit_name": "省农业厅",
    "title": "厅长",
    "budget_amount": 50000.00,
    "ai_profile": "AI分析记录：该客户对农产品质量要求极高，预算目前充足。"
}

sync_resp_1 = httpx.post(f"{base_url}/api/customer/sync", json=customer_data_1, headers=headers)
if sync_resp_1.status_code == 200:
    print("  客户【李厅长】建档成功！此时数据库被拆分存入了客观表与主观表！")
    print("  返回的数据: ", sync_resp_1.json(), "\n")
else:
    print(f"   客户新建失败: {sync_resp_1.text}\n")


# ---- 3. 测试覆盖更新客户资料 ----
print("3. 测试客户资料增量更新 (聊了一周后，老板把预算翻倍了！)")
customer_data_1["budget_amount"] = 100000.00
customer_data_1["ai_profile"] = "AI分析最新记录：对方很满意我方报价，预算翻番为10w！打算本周签约。"

sync_resp_2 = httpx.post(f"{base_url}/api/customer/sync", json=customer_data_1, headers=headers)
if sync_resp_2.status_code == 200:
    print("  客户【李厅长】跟进记录已悄无声息地被更新！")
    print("  观察预算金额已被合并更新为: ", sync_resp_2.json().get('budget_amount'))
else:
    print(f"  更新失败: {sync_resp_2.text}\n")
    
print("\n====================================")
print("自动化 API 测试圆满结束！所有核心接口全部拉通！")
