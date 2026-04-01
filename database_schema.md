# 微信 AI 助手 - 数据库设计表 (Schema V2)

这套数据库架构采用了**“客观事实与主观属性分离”**（即方案 B）的设计，保证了外部关联高可用且支持多员工服务同一客户不串台的情况。

> [!NOTE] 
> 约定说明：
> - `PK`: Primary Key (主键)
> - `AI`: Auto Increment (自增)
> - `FK`: Foreign Key (外键)
> - `Index`: 数据库索引，用于加速查询

---

### 1. User (users) - 工作人员表
| 字段名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| **id** | int | PK, AI | 内部自增主键 |
| **username** | varchar(50) | Unique, Not Null | 登录账号 |
| **password_hash** | varchar(255) | Not Null | 加密后的密码 |
| **real_name** | varchar(50) | Not Null | 真实姓名 |
| wechat_id | varchar(100) | Unique, Nullable | 员工的微信/企微唯一标识 |
| role | varchar(20) | Default 'staff' | 角色权限：admin（管理）/ staff（员工）|
| is_active | boolean | Default True | 账号是否启用（可用于员工离职封禁） |

---

### 2. Customer (customers) - 客观事实表
此表只维护“换了哪个销售去跟进都不会变”的客户物理属性。
| 字段名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| **id** | int | PK, AI | 内部自增主键（系统内标准关联对象） |
| **phone** | varchar(20) | Unique, Index, Not Null | 客户手机号（业务唯一标识） |
| **unit_name** | varchar(100) | Not Null | 客户所在单位真实名称 |
| **customer_name** | varchar(50) | Not Null | 客户真实姓名 |
| unit_type | varchar(50) | Nullable | 单位类型（如：政府部门、国企、私企） |
| admin_division | varchar(100) | Nullable | 所在行政区划 |
| external_id | varchar(50) | Nullable | 外部平台（如832）同步来的唯一身份ID |

---

### 3. UserCustomerRelation (user_customer_relations) - 归属与属性表
此表维护特定的员工去对接一个特定的客户时，产生的**主观跟进记录**。
| 字段名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| **id** | int | PK, AI | 自增主键 |
| **user_id** | int | FK -> users.id, Not Null | 是哪个员工在跟进？ |
| **customer_id**| int | FK -> customers.id, Not Null | 跟进的是哪位客户？ |
| relation_type| varchar(20) | Default 'active' | 归属状态（active：跟进中，transferred：已移交） |
| title | varchar(50) | Nullable | 员工私有的称呼方式（如：王处长） |
| budget_amount| numeric(12,2)| Default 0.00 | 该客户给此员工透露的采购预算金额 |
| contact_date | date | Not Null | 这位员工和客户首次建联时间 |
| ai_profile | text | Nullable | AI分析这位员工的聊天记录得出的动态客户画像 |
| assigned_at | datetime | Not Null | 线索分发给此员工的时间 |

---

### 4. Order (orders) - 订单流水表
| 字段名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| **id** | int | PK, AI | 自增主键 |
| **customer_id**| int | FK -> customers.id, Not Null | 是哪位客户下的单？ |
| user_id | int | FK -> users.id, Nullable | 这**笔订单的业绩属于哪个员工？** (新增) |
| order_date | datetime | Not Null | 订单成交时间 |
| product_title| varchar(255) | Not Null | 购买的商品标题 |
| amount | numeric(12,2)| Not Null | 成交实付金额 |
| category_name| varchar(50) | Nullable | 业务采购分类（如：扶贫助农、办公用品） |
| external_order_id | varchar(50) | Unique, Nullable | 外部商城（832平台）带来的同步订单号 |

---

### 5. ChatMessage (chat_messages) - 聊天记录表
这是 Dify 模型的上下文弹药库。
| 字段名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| **id** | int | PK, AI | 自增主键 |
| **customer_id**| int | FK -> customers.id, Not Null | 针对哪位客户的发言？ |
| user_id | int | FK -> users.id, Nullable | 哪位员工的发言？（如果是纯AI回复可为空） |
| role | varchar(20) | Not Null | 发言者角色（user:客户，assistant:员工/AI） |
| content | text | Not Null | 聊天的具体内容文字 |
| dify_conv_id | varchar(100) | Nullable | 存入 Dify Context 的唯一会话会话ID |
| created_at | datetime | Not Null | 消息发送时间 |

---

### 6. Product (products) - 资源公用表
| 字段名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| **id** | int | PK, AI | 自增主键 |
| uuid | varchar(50) | Unique, Nullable | 832 内部系统同步用的 UUID |
| product_id | varchar(50) | Not Null | 商品编号 |
| product_name | varchar(255) | Not Null | 商品完整名称 |
| price | numeric(10,2)| Not Null | 商品售卖价格 |
| cover_img | varchar(255) | Nullable | 缩略图主图链接 |
| unit | varchar(20) | Nullable | 商品规格计量单位 |
| supplier_name| varchar(100) | Nullable | 供货商名称 |

---

### 7. SystemConfig (system_configs) - 动态配置表
| 字段名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| **id** | int | PK, AI | 自增主键 |
| **config_key** | varchar(100) | Unique, Not Null | 系统变量名（如：dify_api_key） |
| **config_value**| text | Not Null | 系统变量真实内容 |
| config_group | varchar(50) | Default 'general' | 组别 |
| description | varchar(255) | Nullable | 配置的备注说明 |
| updated_at | datetime | Not Null, OnUpdate | 上次参数变更的时间 |
