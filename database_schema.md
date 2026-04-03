# 数据库核心表结构 (v2026.04)

本项目采用了 FastAPI + SQLAlchemy 2.0 (Async) + MySQL 架构。目前数据库包含以下 7 张核心业务/系统表：

### 1. 员工表 (`users`)
基于 RBAC 的系统用户库，记录内部员工及管理员账号。
- `id`: PK
- `username`: 登录账号/工号 (对应前端关联主键)
- `password_hash`: 加密密码
- `real_name`: 真实姓名
- `wechat_id`: 预留绑定的微信号
- `role`: 角色 (`admin` 或 `staff`)
- `is_active`: 启停状态

### 2. 客观客户资料表 (`customers`)
只存储客户的纯客观实体及静态属性，不含任何主观评判。
- `id`: PK
- `phone`: 核心自然键（唯一），供其它表做业务逻辑关联，支持级联更新
- `unit_name`: 挂靠单位名称
- `customer_name`: 客户真实姓名
- `unit_type`: 单位类型（如：预算单位）
- `admin_division`: 行政区划

### 3. 多对多自然挂载关系表 (`user_customer_relations`)
员工与客户之间动态的、强主观色彩的交互与维护节点库，承载 AI 画像及业务状态：
- `id`: PK
- `username`: FK (关联 `users.username`，级联更新)
- `customer_phone`: FK (关联 `customers.phone`，级联更新)
- `title`: 员工专属的客户称呼
- `budget_amount`: 该员工掌握的本单客户预算
- `ai_profile`: Dify 大模型根据会话历史总结的客户精准画像动态文本
- `dify_conversation_id`: Dify 连续对话 ID (实现跨系统跨周期接力记忆，支持业务移交)

### 4. 订单全链路追踪表 (`orders`)
基于业务维度的履约流水与分析（B端长周期履约）。
- `order_id`: 订单全局追踪 ID
- `consignee_phone`: FK (关联 `customers.phone`，级联更新)
- `store`: 关联承接店铺
- `pay_amount`, `freight`, `pay_type_name`: 财务交易镜像数据

### 5. AI 系统协同对话表 (`chat_messages`)
记录与特定客户相关的助理会话，与 Dify 数据对接做双轨制备份共存。
- `role`: 角色区分 (user / ai)
- `content`: 具体内容
- `dify_conv_id`: 会话标志，防止流转时丢失历史消息

### 6. 商品全量公海资源池 (`products`)
来自云端采集引擎同步回来的业务货源物料库。
- `product_id`: B端货源识别码
- `product_name`: 商品全称
- `price`: 大宗集采指导单价
- `cover_img`: 本地防盗链静态资源路径 (结合 L1/L2 桌面缓存使用)
- `product_url`: 外部平台直达详情链接
- `supplier_name`: 供货单位全称
- `supplier_id`: **(NEW)** 用于云端同步开关及过滤检索的供方逻辑隔离主键，与系统配置强行绑定实现动态加载。

### 7. 动态系统配置表 (`system_configs`)
实现无接触式系统热配置核心存储。
- `config_key`: 全局唯一键（如 `supplier_ids`, `sync_status`）
- `config_value`: 键值实体
- `config_group`: 配置隔离组别（`ai`, `sync`, `general`）
- `updated_at`: **(NEW)** 哨兵时间戳，主要用于桌面端捕捉后台同步的进度和监控指标。
