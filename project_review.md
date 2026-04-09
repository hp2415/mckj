# 微企 AI 助手 — 全面项目审查报告

## 项目概览

这是一个面向 B2B 销售场景的"微信 AI 助手"系统，包含三个核心子系统：

| 子系统 | 技术栈 | 主要职责 |
|--------|--------|----------|
| **Backend** | FastAPI + SQLAlchemy (Async) + MySQL | REST API、定时任务、sqladmin 管理后台 |
| **Desktop** | PySide6 + qasync + httpx | 贴附微信的桌面端 AI 对话助手 |
| **Admin** | sqladmin (Bootstrap) | 数据审计、客户管理、业务移交 |

代码总量约 **~3000 行 Python** + 9KB QSS，属于早期 MVP 阶段，功能已经比较完整。

---

## ✅ 优点

### 1. 架构设计合理
- **前后端分离** 清晰：桌面端通过 JWT + REST API 与后端对接，职责边界明确
- **异步全栈**：后端全面采用 `async/await`（aiomysql），桌面端用 `qasync` 融合 Qt 与 asyncio 事件循环，没有阻塞 UI 的同步调用
- **客观/主观数据分离**：`Customer`（客观事实）与 `UserCustomerRelation`（主观跟进）的拆分设计符合业务建模的最佳实践

### 2. 安全意识到位
- JWT 身份认证贯穿所有 API 端点
- 密码使用 Bcrypt 哈希存储，管理后台编辑时自动加密
- 桌面端本地缓存用 Fernet 对称加密 + 用户隔离存储
- 管理后台独立鉴权且限制 admin 角色

### 3. 业务功能完备
- AI 流式对话（Dify SSE 协议）、消息评价与采纳统计
- 832 平台商品自动爬取与定时同步
- 三级图片缓存（L1 内存 → L2 SQLite → L3 网络）
- 微信聊天记录 Excel 导入与客户匹配
- 业务移交（客户批量转移）含审计日志

### 4. 管理后台功能丰富
- 多维度下钻链接（员工→关系→对话）
- 自定义搜寻引擎（`search_query` 重写）解决了 sqladmin 原生 JOIN 别名冲突
- 系统配置表驱动的动态字典（单位类型、区划、采购类型）

---

## ⚠️ 问题与风险

### P0 — 必须立即修复

#### 1. 数据库凭据硬编码
```python
# database.py:7
DATABASE_URL = "mysql+aiomysql://root:root@localhost:3306/ai_assistant_db"
```
- **风险**：root 密码明文写死在代码中，且已提交到 Git
- **建议**：迁移到 `.env` 文件，用 `python-dotenv` 或 Pydantic Settings 管理

#### 2. JWT 密钥硬编码
```python
# core/security.py:6
SECRET_KEY = os.getenv("SECRET_KEY", "fastapi_sqladmin_ai_assistant_secure_key_123")
# core/admin_auth.py:38
admin_auth = AdminAuth(secret_key=os.getenv("SECRET_KEY", "admin-secret-key-xyz-123"))
```
- **风险**：两处 Secret Key 不一致且 fallback 值是弱密钥
- **建议**：统一为一个环境变量，部署时必须显式设置

#### 3. crud.py 存在死代码导致逻辑不可达
```python
# crud.py:186-190 — 重复的 return 语句会导致后续代码不可达
    await db.commit()
    return True

    await db.commit()   # ← 这两行永远不会执行
    return True
```
- **建议**：删除第 189-190 行的重复代码

#### 4. 微信聊天上传接口使用已删除的字段
```python
# api/customer.py:152 — 仍然引用已物理删除的 username 字段
rel_stmt = select(UserCustomerRelation).where(UserCustomerRelation.username == current_user.username)
```
- **风险**：调用 `/api/customer/upload_wechat` 会触发运行时 SQL 错误
- **建议**：改为 `UserCustomerRelation.user_id == current_user.id`

---

### P1 — 短期应修复

#### 5. 所有 HTTP 请求都创建新连接
```python
# api_client.py — 几乎所有方法都 `async with httpx.AsyncClient() as client:`
async with httpx.AsyncClient(timeout=10.0) as client:
    resp = await client.get(url, headers=headers)
```
- **问题**：每次 API 调用都建立新的 TCP 连接 + TLS 握手，性能差
- **建议**：使用类级别的持久 `httpx.AsyncClient` 连接池（`desktop/main.py` 中已有 `_http_session` 但仅用于图片加载）

#### 6. 缺少数据库连接池配置
```python
# database.py:9
engine = create_async_engine(DATABASE_URL, echo=False)
# 未设置 pool_size, max_overflow, pool_recycle 等参数
```
- **建议**：添加 `pool_size=10, max_overflow=20, pool_recycle=3600` 等参数，防止连接泄漏

#### 7. N+1 查询问题
```python
# crud.py:110-113 — 在 for 循环内执行订单聚合查询
for customer, relation in result.all():
    order_stmt = select(func.sum(...)).where(Order.consignee_phone == customer.phone)
    order_res = await db.execute(order_stmt)
```
- **问题**：如果员工有 N 个客户，就会执行 N+1 次数据库查询
- **建议**：用子查询或 JOIN 将订单聚合合并到主查询中

#### 8. selectin 全局预加载的性能隐患
```python
# models.py — User 模型
relations = relationship("UserCustomerRelation", back_populates="user", lazy="selectin")
chat_messages = relationship("ChatMessage", back_populates="user", lazy="selectin")
```
- **问题**：每次加载一个 User 对象都会立即拉取其全部 `chat_messages`，随着数据增长会严重拖慢性能
- **建议**：将模型层改回 `lazy="select"`（默认延迟加载），仅在 admin_views 的 `column_select_related_list` 中按需预加载

#### 9. API 返回格式不统一
```python
# 有些用 HTTPException  (auth.py)
raise HTTPException(status_code=403, detail="权限不足")
# 有些用自定义 dict (customer.py)
return {"code": 403, "message": "权限不足，仅限管理员操作"}
# 有些用 "msg" 而非 "message" (system.py)
return {"code": 403, "msg": "权限不足"}
```
- **建议**：统一为一种响应包装格式，例如 `{"code": int, "message": str, "data": Any}`

#### 10. admin_views.py 重复定义
```python
# UserAdmin 中 column_labels 和 column_select_related_list 各定义了两次
# 第 25-34 行定义一次，第 58-68 行又定义一次（后者覆盖前者）
column_labels = {...}  # 第 25 行
...
column_labels = {...}  # 第 58 行 — 覆盖上面的
```
- **建议**：移除冗余的第一次定义，保持代码整洁

---

### P2 — 中期建议

#### 11. 缺少自动化测试
- 项目完全没有测试文件（无 `tests/` 目录、无 `pytest.ini`）
- **建议**：至少为 CRUD 核心函数和 API 端点编写基础测试

#### 12. 缺少数据库迁移管理
- 虽然有 `alembic/` 目录和 `alembic.ini`，但实际的 schema 变更都通过手写 `ALTER TABLE` 脚本完成
- 项目目录中残留了多个一次性修复脚本（`fix_chat_search_fields.py`、`fix_db_relations.py`、`revert_relation_db.py`、`normalize_relations_db.py`）
- **建议**：
  - 清理这些一次性脚本
  - 使用 Alembic 正式管理 schema 变更
  - 避免手动 DDL 操作

#### 13. 桌面端 main.py 过于臃肿
- `desktop/main.py` 共 551 行，`DesktopApp` 类承担了所有职责（登录、客户管理、AI 对话、商品搜索、同步状态...）
- **建议**：按功能域拆分为多个 Controller / Handler 模块

#### 14. 日志与错误处理不够统一
```python
# 有些地方用 logger
logger.error(f"抓取失败: {sync_error}")
# 有些地方用 print
print(f"AI 回复已落盘，ID: {msg_id}")
# 有些地方静默吞掉异常
except Exception:
    pass
```
- **建议**：统一使用 loguru logger，消除所有 `print()` 和裸 `except: pass`

#### 15. 订单表外键设计不规范
```python
# models.py:56 — 订单使用手机号作为外键
consignee_phone = Column(String(20), ForeignKey("customers.phone", onupdate="CASCADE"))
```
- **问题**：使用自然键（手机号）作为外键，如果客户换号则所有历史订单的关联关系都会断裂
- **建议**：长期应改为 `customer_id` (Integer FK)，与关系表保持一致

---

### P3 — 长期提升

#### 16. 缺少 CORS 配置
- 如果未来要从浏览器端访问 API，需要配置 `CORSMiddleware`

#### 17. 密码字段在管理后台暴露
- `UserAdmin` 的 `column_labels` 中保留了 `password_hash` 列，管理员可以在列表页看到哈希值
- **建议**：将 `password_hash` 从 `column_list` 中移除，仅在编辑表单中显示

#### 18. 部署准备不足
- 没有 `Dockerfile`、`docker-compose.yml` 或任何部署配置
- `requirements.txt` 存在但未验证是否完整与版本锁定
- **建议**：准备容器化部署方案

#### 19. 前端 UI 可考虑迁移
- `desktop/ui/main_window.py` 有 **59KB / ~1500 行**，UI 逻辑与业务逻辑高度耦合
- 如果后续需要跨平台或 Web 化，可考虑将 UI 层迁移到 Electron 或 Web 技术栈

---

## 🏗 架构图

```mermaid
graph TD
    subgraph Desktop["桌面端 (PySide6)"]
        UI["main_window.py<br/>59KB 巨型 UI"]
        APP["main.py<br/>DesktopApp 控制器"]
        API["api_client.py<br/>HTTP 通讯器"]
        STORE["storage.py<br/>加密本地缓存"]
    end

    subgraph Backend["后端 (FastAPI)"]
        MAIN["main.py<br/>应用入口"]
        AUTH["api/auth.py<br/>JWT 鉴权"]
        CUST["api/customer.py<br/>客户 API"]
        PROD["api/product.py<br/>商品 API"]
        SYS["api/system.py<br/>系统 API"]
        CRUD["crud.py<br/>业务逻辑"]
        MODELS["models.py<br/>8 张表"]
        ADMIN["admin_views.py<br/>8 个视图"]
        TASKS["core/tasks.py<br/>定时爬虫"]
    end

    subgraph External["外部服务"]
        DIFY["Dify AI<br/>流式对话"]
        E832["832 平台<br/>商品数据"]
        MYSQL["MySQL<br/>主数据库"]
    end

    UI --> APP --> API --> AUTH
    API --> CUST
    API --> PROD
    API --> SYS
    API --> DIFY
    CUST --> CRUD --> MODELS --> MYSQL
    TASKS --> E832
    TASKS --> MYSQL
    ADMIN --> MODELS
    STORE -.-> |SQLite| APP
```

---

## 📋 建议优先级排序

| 优先级 | 事项 | 预估工时 | 影响面 |
|--------|------|----------|--------|
| **P0** | 修复死代码 (crud.py L189-190) | 5 分钟 | 潜在逻辑错误 |
| **P0** | 修复微信上传接口引用已删除字段 | 10 分钟 | 功能不可用 |
| **P0** | 数据库凭据 + JWT 密钥外部化 | 30 分钟 | 安全漏洞 |
| **P1** | API 响应格式统一化 | 1 小时 | 维护性 |
| **P1** | HTTP 连接池复用 | 1 小时 | 性能 |
| **P1** | N+1 查询优化 | 2 小时 | 性能 |
| **P1** | selectin 预加载收窄到 admin 层 | 1 小时 | 性能 |
| **P1** | admin_views.py 去重/整理 | 30 分钟 | 代码质量 |
| **P2** | 清理一次性修复脚本 | 15 分钟 | 代码卫生 |
| **P2** | 测试基础设施搭建 | 半天 | 质量保障 |
| **P2** | 桌面端控制器拆分 | 半天 | 可维护性 |
| **P3** | 容器化部署 | 半天 | 运维 |

---

## 总结

这个项目作为 MVP 阶段的业务工具，**功能完整度很高**，核心流程（客户管理→AI 对话→订单审计→商品推送）已经跑通。异步架构的选型也很正确。

当前最需要关注的是 **安全加固**（凭据外部化）和 **代码卫生**（死代码、重复定义、一次性脚本清理），这些都是低成本高收益的改善。

性能方面，随着数据量增长，`selectin` 全局预加载和 N+1 查询会成为瓶颈，建议在用户量突破 50 人或对话记录超过 10 万条前完成优化。
