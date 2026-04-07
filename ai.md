# AI 自我进化闭环：从“聊天工具”到“金牌销售大脑”

根据您的设想，我们不再让大模型死记硬背一切，而是采取**“知识库做共有智慧池，上下文做局部记忆池”**的设计。结合微信历史记录的冷启动需求，本次重构计划如下：

## User Review Required

> [!IMPORTNAT]
> 本次规划涉及新增数据表（微信历史记录库）以及对接 Dify 原生的 Dataset（知识库）上传接口。您需要在 Dify 中提前建立一个空的“公共知识库（供全员调用）”，并准备好其 API Key。

## Proposed Changes

---

### 第一阶段：聊天记录离线回流 (WeChat History Integration)

#### [NEW] 数据库扩容：`backend/models.py`
- 新增 `WechatHistory` 模型表，包含字段：时间 (`chat_time`)、聊天内容 (`content`)、发送方/接收方方向定标、对应客户 ID 和 销售 ID。

#### [NEW] 解析接口：`backend/api/customer.py`
- 新增 `POST /api/customer/upload_wechat` 接口。
- 支持直接上传符合您截图格式的 Excel/CSV 文件。后端会根据文件中的 “销售微信名” 去硬匹配系统内的 `User`，根据 “客户备注名” 去宽泛匹配（比如 `LIKE %台州市%`）去挂载到特定的 `Customer` 名下。

---

### 第二阶段：精准上下文强制注射 (Context Injection)

#### [MODIFY] 引擎核心：`backend/api/dify_relay.py` (假设 Dify 网关在此) 或 `desktop/api_client.py` 
- 在启动对特定客户的对话前，先调取该客户最近 20 条 `WechatHistory` 以及数据库内的 `ai_profile`。
- 将这些内容合成一大段结构化 JSON，通过 `inputs` 变量传入 Dify。
- **效果**：大模型每次开口前，脑子里已经装满了这个客户的历史跟进进度，绝对不会像闲聊机器人那样重头“您好，请问您是谁”。

---

### 第三阶段：桌面端 RAG 私有知识库的“点石成金”

#### [MODIFY] 桌面端 UI：`desktop/ui/main_window.py` 组件 `ChatBubble`
- 在每条 AI 或销售自己发送的聊天气泡下方，新增两个微小图标：👍 (标记为金牌话术)、👎 (避坑记录)。
- **交互逻辑**：当销售点击 👍 时，弹窗让销售为其打个 Tag（比如：催单话术、异议处理）。然后桌面端打 API 传给后端。

#### [NEW] 后端 RAG 更新：`backend/api/knowledge.py`
- 接收桌面的点赞话术。
- **神级连招**：FastAPI 直接发起 `POST /v1/datasets/{dataset_id}/document/create_by_text` 调用 Dify 的私密 API，将这条语料作为新的 `Document` 片段直接灌进 Dify 的向量检索库中。
- **效果**：今天某个销冠在聊天框里创造了一段解决“客户觉得运费贵”的话术并点赞，明天全公司的 AI 助理在面对极度相似的问题时，就都会调用这段话术！实现了真正的群体智力共享。

## 验证计划 (Verification Plan)
1. 准备一个您截图里的 Excel 文件。在系统中通过接口完成上传，随后检查数据库 `WechatHistory` 是否自动落盘。
2. 在桌面端给任意一句话点击 👍，随后去 Dify 后台管理面板刷新，若看到该句话已经成为了一条“已分块的文档”，即打通全局成长通道。
