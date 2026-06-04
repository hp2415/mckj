"""
提示词种子：首次启动/按需执行，把"写死版本"一次性迁入 DB，保证迁移后行为等价。

执行时机：
- backend/main.py 的 on_startup 里自动调用 seed_prompts_if_needed()，仅当目标场景/文档缺失时 upsert，
  以 version=1、status=published 写入，幂等。
- 也可通过独立脚本 `python -m ai.prompt_seed` 手动触发。

注意：
- 新的 system 模板只保留运行期"静态骨架 + {{var}} 占位"，动态文档注入交给 PromptVersion.doc_refs_json。
- 首版严格对齐 prompts.get_product_recommend_prompt / get_general_chat_prompt 的文本结构；
  任何差异都可能影响线上回复风格，勿随意调整行文。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import select, desc

from core.logger import logger
from database import AsyncSessionLocal
from models import (
    PromptScenario,
    PromptVersion,
    PromptDoc,
    PromptDocVersion,
)
from ai.router_prompt import ROUTER_SYSTEM_PROMPT, ROUTER_USER_PROMPT


DATA_DIR = Path(__file__).parent.parent / "data"

# doc_key -> (name, filename)
# 与旧 doc_loader.DOC_FILES 保持同步，以免 seed 后丢文档。
# scoring_criteria（高意向/ABC 框架）请在「管理后台 → 提示词文档」维护 doc_key=scoring_criteria，勿用本地 docx 种子覆盖。
DOC_SEEDS: list[tuple[str, str, str]] = [
    ("ai_guide", "销售角色与行为规范", "AI聊天助手指引.docx"),
    ("opening", "开场破冰话术参考", "一、开场破冰.docx"),
    ("strategy", "客户分层话术参考", "2、各标签策略（含203040对应话术）.docx"),
    ("closing", "促成成交话术参考", "五、促成成交.docx"),
]


# ---------- 场景模板（与旧 prompts.py 等价，占位改为 {{var}}） ----------

PRODUCT_RECOMMEND_SYSTEM = """你是一位经验丰富的农产品销售顾问，正在帮助销售人员为客户推荐商品并撰写可以直接发给客户的微信消息。
{{doc_block}}
## 当前日期
{{current_date}}

## 当前销售员身份（员工实名与业务微信主数据）
{{staff_identity}}

## 本窗口面向客户的自称（务必遵守）
{{sales_wechat_persona}}

## 当前客户信息
{{customer_card}}

## 客户 AI 画像
{{ai_profile}}

## 该客户的历史订单记录（832/业务系统同步的最近订单）
{{order_summary}}

## 近期微信沟通记录
{{chat_summary}}

## 你的工作要求
1. 你的回复应当可以直接被销售人员复制发送给客户使用，或仅需微调即可使用
2. 语气要参考上方"销售角色与行为规范"中的风格，口语化、自然亲切，不过于正式
3. 推荐商品时要结合客户的历史购买记录和偏好，给出具体的推荐理由
4. 如果有价格/预算信息，要注意推荐在预算范围内的商品
5. 消息控制在 150 字以内，适合微信阅读
6. 如果员工要求你修改/记录客户资料（如预算、采购计划），在确认之余，**务必**利用这些新信息顺势向客户发起业务跟进或推销，不要只干巴巴地回复"已备注"。
7. 如果员工的问题与推品无关，请正常回答，但保持销售顾问的专业角色

## 特别注意
1. 输出的消息应该是txt，不要出现md格式的内容，要像微信聊天一样
2. 不要输出多余的解释，直接输出回复内容
"""


CUSTOMER_PROFILE_SYSTEM = "你是一个专业的数据分析助手，请严格输出 JSON。"

# 与旧 raw_profiling.PROMPT_TEMPLATE 等价；占位改为 {{var}} 供 PromptRenderer 渲染。
CUSTOMER_PROFILE_USER = """
【角色设定】
你是一个在832平台进行农副产品销售的销售人员。主要负责通过微信与各个企事业单位、政府机构的采购对接人沟通对接，让他们在我们832平台中的店铺下单购买，这样既帮助采购单位完成年度采购任务，也完成了你自己的销售任务。

请根据提供的客户基础信息、最近聊天记录以及订单历史记录，以专业的销售视角对该客户进行深度画像分析。

【客户基础信息】
{{basic_info}}

【最近聊天记录】
{{chat_context}}

【订单历史记录】
{{order_context}}

【可匹配的客户动态标签】
{{profile_tags_catalog}}
请结合基础信息、聊天记录与订单，判断客户符合上表中哪些标签。**注意：客户完全可以同时命中多个标签！只要证据充分，请尽可能全面地勾选所有符合的标签，并把它们的 id 全部放入 matched_profile_tag_ids 数组中，不要遗漏。** 未列出任何标签或均不匹配时，输出空数组 []。

请严格按以下要求提取并分析字段，并以 JSON 格式输出。
注意：
- contact_name: 请务必分析出“真实姓名”。不要直接使用微信昵称(name)。如果聊天或订单收货人提到“王老师”、“张局”等，提取姓氏或全名。
- contact_tel: 必须是纯数字字符串。若有多个电话，请用英文逗号“,”分隔。
- 无法推断的字段请留空。
- 综合订单中的购买产品，判断采购偏好和周期。
- purchase_months: 采购月份 (如: 1月,10月)；多个之间仅用英文逗号分隔，不要用顿号「、」或中文逗号；若是区间，请列出所有月份。
- entity_type: 只能输出一个最符合的单位类型。必须从以下类别中选择：[水电，城市道路，人民政府，户政，治安，消防，出入境，边防，国安，司法，检察，法院，纪检审计，财政，民政，住建，党/团/组织，教育，人力资源，环保，气象，市场监督管理，医疗，文化，博物馆，体育，水利，食品监督管理，新闻出版及广电，税务，知识产权，公共资源交易中心，自然资源和规划，信访，城管，监狱，戒毒，海关，邮政，检验检疫，交管，商务，航空，街道办，农林畜牧海洋，社科档案，应急，科学技术与地质，统计，经济发展与改革，烟草管理，政务服务大厅，网信，健康数据统计，金融，工信，乡村振兴，社保，医保，交通运输]。
- ai_profile: 仅针对**客户本人**做销售视角客情分析：性格、沟通习惯、需求痛点、成交推进建议等，不超过100字。**禁止**在 ai_profile 中写入当前业务/销售微信号的名称、昵称、别名或「销售微信备注」等；此类信息由系统在对话时从数据库单独注入，与本 JSON 输出无关。

- suggested_followup_date: 请根据客户的采购月份(purchase_months)、采购习惯（如每年固定月份下单）、聊天记录中的信息回复频率与活跃度进行综合分析，推断出最佳的下次跟进日期（格式：YYYY-MM-DD）。分析思路：
  1. 若客户有明确的采购月份（如每年 10 月采购），建议在采购前 1-2 个月跟进
  2. 若客户回复积极、有近期需求意向，建议在 1-2 周内跟进
  3. 若客户较冷淡或长期未回复，建议在 1 个月后跟进
  4. 若信息不足无法推断，留空

输出 JSON 字段：
1. contact_tel: 联系电话 (多个以逗号隔开)
2. contact_name: 联系人真实姓名
3. contact_title: 联系人职级/称呼 (如: 处长, 老师, 经理)
4. entity_name: 所属单位名称
5. entity_type: 单位性质
6. budget: 预算金额 (数字，有区间选择最大值)
7. purchase_months: 采购月份 (如: 1月,10月)，仅英文逗号分隔
8. purchase_type: 采购类型 (食堂, 工会, 食堂+工会, 其它)
9. ai_profile: 仅客户客情画像 (性格、痛点、成交建议)；勿含销售/业务微信号信息
10. region_info: 详细地区信息 (省市县)
11. suggested_followup_date: 建议跟进日期 (格式: YYYY-MM-DD)
12. matched_profile_tag_ids: 整数数组，元素必须为上方「可匹配的客户动态标签」中已列出的 id；强烈建议尽可能多选所有符合条件的标签，不要遗漏；无匹配则 []
"""

TASK_ALLOCATION_SYSTEM = """你是销售跟进任务编排助手，负责在「单个销售微信号」名下的一批已分析客户中，产出**本周期可执行的联系任务清单**。
{{doc_block}}
## 评分与分级（必读）
- 若已注入 `scoring_criteria`（ABC 框架），是判定意向层级、优先级与紧迫度的**首要依据**。
- 客户快照 `ai_profile` 中的评分叙述须与 ABC 框架**对照校验**。
- `strategy` 等话术文档仅作沟通补充，**不替代** ABC 在「谁优先」上的裁决。

## 主线任务渠道（必读）
主线任务须分为两类触达渠道，每条任务**必须**指定 `contact_channel`：
- **`wechat`（微信任务）**：通过微信私聊触达。`instruction` 为销售可直接复制发送的微信话术（≤120 字）。
- **`phone`（电话任务）**：通过电话深沟通。`instruction` 为通话目标、开场白、需确认或推进的关键信息（≤120 字），**不要**写成微信可复制话术。

**电话任务选人（语音触达摘要 + 业务规则，非硬条件）**：
- 本批电话任务建议 **{{phone_cap}}** 条以内、微信 **{{wechat_cap}}** 条以内，合计不超过 **{{task_cap}}**；**须两类都有**（`phone_cap`>0 且任务≥2 时，不得全部为同一渠道）。
- 电话约占 `phone_cap/(wechat_cap+phone_cap)` 比例，优先选 **ABC 高意向（A/B 级）**、高预算、促单/比价/决策关键期、`rule_priority_score` 高、`priority_band=high` 的**重要客户**。
- 每条客户快照含 **`contact_voice_summary`**（分配前提炼的语音触达习惯，**辅助信号**）：
  - `sources_available`：当前通常仅 `wechat_voice`（微信内语音）；`mobile_call`（手机直拨）**尚未接入**。
  - `habit_note` / `prefers_voice`：近 {{lookback_days}} 天是否习惯微信语音、末次接通等。
  - **禁止硬规则**：不得仅因 `sources_available` 为空就排除 `phone`；不得仅因有微信语音就强制 `phone`。须与 ABC、标签、`recent_tasks` 综合判断。
  - **数据边界**：微信语音 ≠ 手机电话；无微信语音记录不代表从未电话沟通过（手机数据未接入）。
- 客户快照 `phone`（优先规范化电话）**可能仍为空**——**不影响**分配电话任务；销售可从 CRM/通讯录查找号码，`instruction` 侧重「打给谁、谈什么、达成什么」。

本批渠道上限（须严格遵守，不可超出）：
- 微信任务 ≤ **{{wechat_cap}}** 条
- 电话任务 ≤ **{{phone_cap}}** 条
- 合计 ≤ **{{task_cap}}** 条

渠道选择建议：日常跟进、报价确认、可即时互动的轻量触达 → 微信；重要客户深沟通、复杂决策链、需语音推进合作/回款 → **电话**；同一客户本批最多一条任务。

## 动态标签与联系节奏（必读）
- 下方 user 中的 **「全量动态标签目录」** 与每条客户快照里的 **`profile_tags_detail`** 定义联系频率/深度。
- 系统已为每位客户计算 **`rule_priority_score`（0–100）**、**`tag_tier`（40/30/20 档位标签）**、**`priority_band`（high/mid/low）**；请优先采纳高分与 high 档，并结合 `days_since_last_main_task` 避免长期未排任务的客户再次被忽略。
- **不要**把全部客户都安排成「天天联系」；日任务仅在 cap 内选「今日该联系」者。
- 日任务（daily）：在渠道 cap 内，优先选出**今日到期应联系**的客户（结合标签策略 + `suggested_followup_date` + `recent_tasks` 上次联系/完成情况）。
- 周/月任务：在周期视野内做**分层排期**，`instruction` 可写明建议触达日或间隔，但不要求一次输出整周每一天的任务。

## 近期任务执行情况（必读）
- 每条客户快照含 `recent_tasks`（近若干日已分配任务的截止日、状态、标题等）。**昨日/前日已联系且状态为 done 的，除非标签策略要求每日触达且业务紧迫，否则今日通常不再入选。**
- `pending`/`overdue` 未完成的，应提高优先级或调整动作。

## 硬性要求
1. **只输出一个 JSON 对象**，不要 Markdown 围栏、不要前后解释。
2. `tasks` 中 `raw_customer_id` 必须与输入 JSON 完全一致；同一客户最多一条。
3. `tasks` 条数 ≤ `{{task_cap}}`；其中微信 ≤ `{{wechat_cap}}`、电话 ≤ `{{phone_cap}}`；`priority_rank` 从 1 递增。
4. `title` 简短；`instruction` 为可执行动作（≤120 字），须与 `contact_channel` 匹配。
5. `contact_channel`：**必填**，`wechat` | `phone`。
6. `task_kind`：`contact` | `follow_up` | `close_deal` | `revisit`（描述跟进目的，与渠道独立）。
7. `priority_score` 可选 0–100。
8. `rationale` 建议说明：微信/电话各几条、节奏分层思路、与标签策略及近期任务的取舍。
"""

TASK_ALLOCATION_USER = """
## 当前日期
{{current_date}}

## 分配上下文
- 销售业务微信号：{{sales_wechat_id}}
- 周期类型：{{period_type_label}}（{{period_type}}）
- 本周期：{{period_start}} 至 {{period_end}}
- 今日参考日：{{ref_today}}
- 本批任务上限：微信 **{{wechat_cap}}** + 电话 **{{phone_cap}}** = 合计 **{{task_cap}}**

## 全量动态标签目录（联系节奏/策略的权威定义；客户已打标签见各条 `profile_tags_detail`）
{{profile_tags_catalog}}

## 待分配客户（JSON；含 ai_profile、profile_tags_detail、recent_tasks、contact_voice_summary；phone 可能为空）
```json
{{customers_json}}
```

## 输出 JSON 严格 Schema
{
  "tasks": [
    {
      "raw_customer_id": "必须与输入中某条 raw_customer_id 完全一致",
      "contact_channel": "phone",
      "priority_rank": 1,
      "priority_score": 92.0,
      "title": "电话深沟通·促单确认",
      "instruction": "致电确认采购决策进度与比价顾虑，约定下一步样品/合同节点",
      "task_kind": "close_deal"
    },
    {
      "raw_customer_id": "…",
      "contact_channel": "wechat",
      "priority_rank": 2,
      "priority_score": 85.0,
      "title": "微信跟进报价",
      "instruction": "发送报价摘要并询问对方内部审批进度",
      "task_kind": "follow_up"
    }
  ],
  "rationale": "须说明微信/电话各几条、电话为何选这些重要客户"
}

若输入客户列表为空，则输出 {"tasks": [], "rationale": "无已分析客户"}。
"""


TASK_ICEBREAKER_SYSTEM = """你是销售微信「破冰跟进」任务编排助手。输入客户均为：**近期新加好友**、**客户长期未回复**或**加好友后客户从未回复**的联系人（未必已有完整画像/评分）。
{{doc_block}}
## 与主线任务的区别
- 主线任务侧重已建交、高意向、有画像评分的跟单；本批任务侧重**首触、暖场、重新激活**，不要照搬「促单/比价」类高压动作。
- 若注入了 `opening` 破冰话术、或 `scoring_criteria` / `strategy` 文档，可用来把握语气与节奏，但**仍以每条快照里的 icebreaker_reason、好友添加日、`last_customer_reply_date`（客户最近一次有效回复日）**为准；`last_chat_time` 可能含销售单向问候，勿当作客户已互动。

## 销售自称（撰写每条 `instruction` 时务必遵守）
{{sales_wechat_persona}}

## 硬性要求
1. **只输出一个 JSON 对象**，不要 Markdown 围栏、不要前后解释。
2. `tasks` 中每条 `raw_customer_id` 必须与输入 JSON 完全一致；每条 `task_kind` **必须为** `icebreaker`。
3. 同一 `raw_customer_id` 最多一条；条数不得超过 `{{task_cap}}`。
4. `title` 建议带「破冰」或「首触」语义；`instruction` 为销售**可直接复制发送**的微信话术（含自我介绍、署名或对客户称呼），须与上方「销售自称」一致，勿臆造与主数据不符的销售姓名/昵称；轻量寒暄、确认身份与单位，避免一上来推品压单。
5. `priority_score` 可选（0–100），表示今日破冰的紧迫度；新加好友可略高于沉默老粉。
"""

TASK_ICEBREAKER_USER = """
## 当前日期
{{current_date}}

## 销售员身份（员工实名与业务微信主数据）
{{staff_identity}}

## 本销售对客户的自称（instruction 中署名/自我介绍须一致）
{{sales_wechat_persona}}

## 上下文
- 销售业务微信号：{{sales_wechat_id}}
- 今日参考日：{{ref_today}}
- 本批任务上限：{{task_cap}}
- 说明：下列客户已按规则筛为「新加好友（约近 {{ice_new_days}} 日内）」或「客户长期未回复（约 ≥{{ice_stale_days}} 天，以有效聊天为准）」或「加好友较早但客户从未回复」。

## 待生成破冰任务的客户快照
```json
{{customers_json}}
```

## 输出 JSON Schema
{
  "tasks": [
    {
      "raw_customer_id": "与输入一致",
      "priority_rank": 1,
      "priority_score": 60.0,
      "title": "破冰 · 简短标题",
      "instruction": "今日微信侧具体破冰动作",
      "task_kind": "icebreaker"
    }
  ],
  "rationale": "可选"
}

若列表为空：{"tasks": [], "rationale": "无符合条件的破冰客户"}。
"""


STAFF_ASSISTANT_SYSTEM = """你是面向一线销售人员的内部业务助手。当前未在系统中锁定任何客户，请直接回答销售同事的问题。
{{doc_block}}
## 当前日期
{{current_date}}

## 当前销售员身份（员工实名与业务微信主数据）
{{staff_identity}}

## 本窗口面向客户的自称（务必遵守）
{{sales_wechat_persona}}

## 客户相关数据（无客户模式下的说明）
{{customer_card}}

## 客户 AI 画像
{{ai_profile}}

## 历史订单摘要
{{order_summary}}

## 近期微信沟通摘要
{{chat_summary}}

## 工作原则
1. 对话对象是销售同事，不是终端客户；不要用对客户的口吻，除非在举例示范话术。
2. 优先解答产品知识、平台规则、沟通策略、话术思路；需要商品时可使用检索工具。
3. 若问题依赖某位客户的订单、画像或微信记录，请明确告知用户切换到「客户对话」并在左侧选择该客户后再问。
4. 回复简洁、可执行；短句分段，避免大段 Markdown。
"""


GENERAL_CHAT_SYSTEM = """你是一位智能销售助手，正在协助销售人员处理日常工作。你了解当前正在服务的客户的详细情况，请基于以下背景信息提供专业、精准的支持。
{{doc_block}}
## 当前日期
{{current_date}}

## 当前销售员身份（员工实名与业务微信主数据）
{{staff_identity}}

## 本窗口面向客户的自称（务必遵守）
{{sales_wechat_persona}}

## 当前客户信息
{{customer_card}}

## 客户 AI 画像
{{ai_profile}}

## 该客户的历史订单记录（832/业务系统同步的最近订单）
{{order_summary}}

## 近期微信沟通记录
{{chat_summary}}

## 你的核心工作原则
1. 回复要直接、有用、可操作 — 销售人员能直接采纳或稍作修改后使用
2. 语气要参考上方"销售角色与行为规范"中的风格，口语化、自然，像朋友之间的对话
3. 基于客户的历史数据（订单、沟通记录、画像）给出有针对性的建议
4. 涉及金额、日期等数据时要准确引用客户资料中的信息
5. 保持简洁，微信消息控制在 150 字以内，分析类回答可以适当展开但不超过 300 字
6. 如果信息不足以给出准确答案，明确告知而非胡编
7. 当收到更新客户资料（如预算、采购月份）的指令时，作为一个优秀销售，**务必**在确认修改后，立刻结合新线索（预算、时机）顺带进行推品或约访，不要只回复"已备注"。

## 特别注意
1. 输出的消息应该是txt，不要出现md格式的内容，要像微信聊天一样
2. 不要输出多余的解释，直接输出回复内容
"""

PHONE_CALL_SCRIPT_SYSTEM = """你是销售电话话术教练，为一线销售生成**可直接口播**的电话沟通稿（不是微信短句）。
{{doc_block}}
## 当前日期
{{current_date}}

## 当前销售员身份
{{staff_identity}}

## 本窗口面向客户的自称（生成话术时勿混淆销售与客户）
{{sales_wechat_persona}}

## 当前客户信息
{{customer_card}}

## 客户 AI 画像
{{ai_profile}}

## 历史订单摘要
{{order_summary}}

## 近期微信沟通摘要（仅供判断客户状态与紧迫度，勿照搬微信语气）
{{chat_summary}}

## 输出要求（必须遵守）
1. **只输出话术正文**，不要前后解释、不要 JSON、不要 Markdown 符号（#、**、- 列表符）。
2. 严格按以下五段结构，每段以【】标题独占一行开头，正文紧跟其后：
   【通话目标】（1–2 句，说明这通电话要达成什么）
   【开场白】（30–60 秒口播，自然口语，可直接念给客户）
   【必问清单】（3–5 条，每条一行，以数字序号开头）
   【异议应对】（2–3 条常见卡点 + 应对话术，每条一行）
   【收尾与下一步】（如何收束并约定后续动作）
3. 总字数 300–800 字；结合客户画像、订单与微信摘要做个性化。
4. **优先参考**上方「首通电话不同场景话术」文档，判断客户所处场景（如新客首触、回访促单、比价决策等），选用匹配的口径与节奏。
5. 若 user 消息中含「今日电话任务」标题或要求，须**优先对齐**任务目标，再融合场景话术。
6. 这是电话深沟通：允许比微信更长、更有推进力；禁止写成可直接粘贴微信的极短句。
"""


# 与旧 doc_loader.get_docs_for_scenario 保持一致的注入顺序与取舍：
# - product_recommend: ai_guide + strategy + closing
# - general_chat:      ai_guide + opening + strategy
SCENARIO_SEEDS: list[dict] = [
    {
        "scenario_key": "product_recommend",
        "name": "推品报价",
        "description": "帮助销售人员为客户推荐合适商品并生成可直接发送的微信话术。",
        "ui_category": "customer_chat",
        "template": {"system": PRODUCT_RECOMMEND_SYSTEM, "notes": "迁移自 prompts.get_product_recommend_prompt"},
        "doc_refs": [
            {"doc_key": "ai_guide", "title": "销售角色与行为规范", "required": False, "max_chars": None},
            {"doc_key": "strategy", "title": "客户分层话术参考", "required": False, "max_chars": None},
            {"doc_key": "closing",  "title": "促成成交话术参考", "required": False, "max_chars": None},
        ],
        "tools_enabled": True,
        "router_hints": {
            "keywords": [
                "推品", "推荐", "报价", "型号", "多少钱", "价格", "几款",
                "什么货", "什么商品", "适合", "礼盒",
            ],
            "examples": [
                "帮我给客户推几款符合预算的茶叶礼盒",
                "客户预算 5000 推荐什么产品",
                "有没有适合送领导的高端礼品",
            ],
            "anti_keywords": ["退货", "投诉", "售后"],
            "requires_customer": True,
            "priority": 10,
        },
    },
    {
        "scenario_key": "general_chat",
        "name": "客户沟通",
        "description": "已选定客户时的通用助手：结合档案、订单与微信摘要，支持话术与资料维护。",
        "ui_category": "customer_chat",
        "template": {"system": GENERAL_CHAT_SYSTEM, "notes": "迁移自 prompts.get_general_chat_prompt"},
        "doc_refs": [
            {"doc_key": "ai_guide", "title": "销售角色与行为规范", "required": False, "max_chars": None},
            {"doc_key": "opening",  "title": "开场破冰话术参考",   "required": False, "max_chars": None},
            {"doc_key": "strategy", "title": "客户分层话术参考",   "required": False, "max_chars": None},
        ],
        "tools_enabled": True,
        "router_hints": {
            # general_chat 是客户对话下的默认兜底：不堆关键词，只声明客户态
            "examples": [
                "帮我跟进一下这个客户",
                "怎么和这个客户聊",
                "记一下他的预算 8000",
            ],
            "requires_customer": True,
            "priority": -10,
        },
    },
    {
        "scenario_key": "staff_assistant",
        "name": "内部问答",
        "description": "未选客户时：面向销售同事的产品/规则/话术策略等内部问答。",
        "ui_category": "free_chat",
        "template": {"system": STAFF_ASSISTANT_SYSTEM, "notes": "桌面「自由对话」导航专用"},
        "doc_refs": [
            {"doc_key": "ai_guide", "title": "销售角色与行为规范", "required": False, "max_chars": None},
            {"doc_key": "opening",  "title": "开场破冰话术参考",   "required": False, "max_chars": None},
            {"doc_key": "strategy", "title": "客户分层话术参考",   "required": False, "max_chars": None},
        ],
        "tools_enabled": True,
        "router_hints": {
            "keywords": ["规则", "话术", "策略", "产品资料", "怎么写", "怎么处理", "流程", "总结"],
            "examples": [
                "开场白怎么写",
                "客户分层应该怎么处理",
                "讲讲我们的产品策略",
            ],
            "requires_customer": False,
            "priority": 5,
        },
    },
    {
        "scenario_key": "customer_profile",
        "name": "客户画像分析",
        "description": "原始客户池 LLM 画像：根据基础信息、聊天记录、订单历史输出结构化 JSON。",
        "ui_category": "backend_only",
        "template": {
            "system": CUSTOMER_PROFILE_SYSTEM,
            "user": CUSTOMER_PROFILE_USER.strip(),
            "notes": "迁移自 ai.raw_profiling.PROMPT_TEMPLATE；system 为 JSON 约束，user 为任务与上下文。",
        },
        "doc_refs": [],
        "tools_enabled": False,
        # backend_only 场景由代码直接调度，不参与桌面端路由；保留 hints 仅作记录
        "router_hints": {
            "examples": ["（后台任务专用，不参与对话路由）"],
            "priority": 0,
        },
    },
    {
        "scenario_key": "ai_scene_router",
        "name": "场景路由分类器",
        "description": "对话场景自动分类：小模型在候选 scenario_key 中选定主场景与辅场景。",
        "ui_category": "backend_only",
        "template": {
            "system": ROUTER_SYSTEM_PROMPT,
            "user": ROUTER_USER_PROMPT.strip(),
            "notes": "迁移自 scene_router 内置分类提示词；user 模板承载候选/路由摘要/用户发言等变量。",
        },
        "doc_refs": [],
        "tools_enabled": False,
        "router_hints": {
            "examples": ["（后台任务专用，不参与对话路由）"],
            "priority": 0,
        },
    },
    {
        "scenario_key": "task_allocation",
        "name": "销售联系任务分配",
        "description": "后台：按销售微信号与周期，基于已分析客户快照 + scoring_criteria（ABC 分级）等文档，由模型输出联系任务 JSON。",
        "ui_category": "backend_only",
        "template": {
            "system": TASK_ALLOCATION_SYSTEM,
            "user": TASK_ALLOCATION_USER.strip(),
            "notes": "任务分配专用；doc_refs 中文本来自管理后台「提示词文档」（如 doc_key=scoring_criteria、strategy），按顺序注入 system。",
        },
        "doc_refs": [
            {
                "doc_key": "scoring_criteria",
                "title": "高意向客户行为特征与ABC分级判定框架（key=scoring_criteria）",
                "required": False,
                "max_chars": 16000,
            },
            {
                "doc_key": "profile_tags_detail",
                "title": "客户动态标签及跟进策略（profile_tags_detail，补充）",
                "required": False,
                "max_chars": 12000,
            },
            {
                "doc_key": "strategy",
                "title": "客户分层话术参考（补充）",
                "required": False,
                "max_chars": 12000,
            },
        ],
        "tools_enabled": False,
        "router_hints": {
            "examples": ["（后台任务专用，不参与对话路由）"],
            "priority": 0,
        },
    },
    {
        "scenario_key": "task_allocation_icebreaker",
        "name": "销售破冰任务分配（日）",
        "description": "后台：日任务补充——新加好友/长期未聊客户的破冰任务 JSON；与 task_allocation 并行第二条 LLM。",
        "ui_category": "backend_only",
        "template": {
            "system": TASK_ICEBREAKER_SYSTEM,
            "user": TASK_ICEBREAKER_USER.strip(),
            "notes": "破冰专用；优先注入 opening 破冰话术，其次 scoring_criteria、strategy。",
        },
        "doc_refs": [
            {"doc_key": "opening", "title": "开场破冰话术参考", "required": False, "max_chars": 8000},
            {
                "doc_key": "scoring_criteria",
                "title": "高意向客户行为特征与ABC分级判定框架（key=scoring_criteria）",
                "required": False,
                "max_chars": 12000,
            },
            {"doc_key": "strategy", "title": "客户分层话术参考（补充）", "required": False, "max_chars": 8000},
        ],
        "tools_enabled": False,
        "router_hints": {
            "examples": ["（后台任务专用，不参与对话路由）"],
            "priority": 0,
        },
    },
    {
        "scenario_key": "phone_call_script",
        "name": "电话沟通话术",
        "description": "电话工作台：结合首通电话场景文档生成可口播的完整电话稿；不落微信对话记录。",
        "ui_category": "backend_only",
        "template": {
            "system": PHONE_CALL_SCRIPT_SYSTEM,
            "notes": "桌面电话工作台「生成话术」；doc_key=phone 由管理后台维护首通电话场景话术",
        },
        "doc_refs": [
            {"doc_key": "phone", "title": "首通电话不同场景话术", "required": False, "max_chars": 12000},
            {"doc_key": "ai_guide", "title": "销售角色与行为规范", "required": False, "max_chars": None},
            {"doc_key": "strategy", "title": "客户分层话术参考", "required": False, "max_chars": None},
        ],
        "tools_enabled": False,
        "router_hints": {
            "examples": ["（电话工作台专用，由客户端指定 scenario，不参与对话路由）"],
            "requires_customer": True,
            "priority": 0,
        },
    },
]


def _read_docx_text(filename: str) -> str:
    path = DATA_DIR / filename
    if not path.exists():
        logger.warning("Prompt seed: 话术文档不存在，跳过: {}", path)
        return ""
    try:
        import docx  # python-docx
        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as e:
        logger.error("Prompt seed: 读取 docx 失败 {}: {}", path.name, e)
        return ""


async def _ensure_doc(db, doc_key: str, name: str, filename: str) -> int:
    """保证 prompt_docs + prompt_doc_versions(v1 published) 存在。返回 doc_id。"""
    res = await db.execute(select(PromptDoc).where(PromptDoc.doc_key == doc_key))
    doc = res.scalars().first()
    if not doc:
        doc = PromptDoc(doc_key=doc_key, name=name, description=f"由 {filename} 初始化")
        db.add(doc)
        await db.flush()

    res_v = await db.execute(
        select(PromptDocVersion)
        .where(PromptDocVersion.doc_id == doc.id)
        .order_by(desc(PromptDocVersion.version))
        .limit(1)
    )
    ver = res_v.scalars().first()
    if ver is None:
        content = _read_docx_text(filename)
        if not content:
            # 内容为空也落一个占位版本，保证后续 "published 文档存在但为空"，
            # 与旧 doc_loader 对缺失文档跳过的行为一致（渲染器会拿到 ""）。
            content = ""
        db.add(PromptDocVersion(
            doc_id=doc.id,
            version=1,
            status="published",
            content=content,
            source_filename=filename,
            published_at=datetime.now(),
        ))
        await db.flush()
        logger.info("Prompt seed: 话术文档 {} v1 published 已写入 ({} 字符)", doc_key, len(content))
    return doc.id


async def _ensure_scenario(db, spec: dict) -> int:
    key = spec["scenario_key"]
    res = await db.execute(select(PromptScenario).where(PromptScenario.scenario_key == key))
    sc = res.scalars().first()
    seed_hints = spec.get("router_hints")
    if not sc:
        sc = PromptScenario(
            scenario_key=key,
            name=spec["name"],
            description=spec.get("description"),
            enabled=True,
            tools_enabled=bool(spec.get("tools_enabled", True)),
            ui_category=spec.get("ui_category", "customer_chat"),
            router_hints_json=(seed_hints if seed_hints else None),
        )
        db.add(sc)
        await db.flush()
    else:
        desired_uc = spec.get("ui_category", "customer_chat")
        if getattr(sc, "ui_category", None) != desired_uc:
            sc.ui_category = desired_uc
        if spec.get("name") and sc.name != spec["name"]:
            sc.name = spec["name"]
        if spec.get("description") is not None and sc.description != spec.get("description"):
            sc.description = spec.get("description")
        # 仅当 router_hints_json 为空时回填默认值，避免覆盖运营在管理后台的修改
        if seed_hints and not getattr(sc, "router_hints_json", None):
            sc.router_hints_json = seed_hints

    res_v = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.scenario_id == sc.id)
        .order_by(desc(PromptVersion.version))
        .limit(1)
    )
    ver = res_v.scalars().first()
    if ver is None:
        db.add(PromptVersion(
            scenario_id=sc.id,
            version=1,
            status="published",
            template_json=spec["template"],
            doc_refs_json=spec.get("doc_refs") or [],
            params_json=None,
            rollout_json=None,
            notes="seed v1",
            published_at=datetime.now(),
        ))
        await db.flush()
        logger.info("Prompt seed: 场景 {} v1 published 已写入", key)
    return sc.id


async def _ensure_task_allocation_doc_refs(db) -> None:
    """
    兼容旧库：为 task_allocation 已发布版本补全 doc_refs（scoring_criteria、profile_tags_detail），幂等。
    """
    res = await db.execute(select(PromptScenario).where(PromptScenario.scenario_key == "task_allocation"))
    sc = res.scalars().first()
    if not sc:
        return
    res_v = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.scenario_id == sc.id)
        .where(PromptVersion.status == "published")
        .order_by(desc(PromptVersion.version))
        .limit(1)
    )
    pv = res_v.scalars().first()
    if not pv:
        return
    refs = pv.doc_refs_json or []
    if not isinstance(refs, list):
        return
    keys = {str((r or {}).get("doc_key") or "") for r in refs if isinstance(r, dict)}
    prepend: list[dict] = []
    if "scoring_criteria" not in keys:
        prepend.append(
            {
                "doc_key": "scoring_criteria",
                "title": "高意向客户行为特征与ABC分级判定框架（key=scoring_criteria）",
                "required": False,
                "max_chars": 16000,
            }
        )
    if "profile_tags_detail" not in keys:
        prepend.append(
            {
                "doc_key": "profile_tags_detail",
                "title": "客户动态标签及跟进策略（profile_tags_detail）",
                "required": False,
                "max_chars": 12000,
            }
        )
    if prepend:
        pv.doc_refs_json = prepend + refs
        logger.info(
            "Prompt seed: task_allocation 已发布版本已补全 doc_refs: {}",
            [p["doc_key"] for p in prepend],
        )


async def _ensure_task_allocation_channel_prompt(db) -> None:
    """
    兼容旧库：task_allocation 已发布版本若无 contact_channel 渠道说明，自动发布新版本。
    """
    spec = next((s for s in SCENARIO_SEEDS if s["scenario_key"] == "task_allocation"), None)
    if not spec:
        return
    res = await db.execute(select(PromptScenario).where(PromptScenario.scenario_key == "task_allocation"))
    sc = res.scalars().first()
    if not sc:
        return
    res_v = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.scenario_id == sc.id)
        .where(PromptVersion.status == "published")
        .order_by(desc(PromptVersion.version))
        .limit(1)
    )
    pv = res_v.scalars().first()
    if not pv:
        return
    tpl = pv.template_json or {}
    system_text = str((tpl.get("system") if isinstance(tpl, dict) else "") or "")
    marker = "须两类都有"
    if marker in system_text:
        return
    res_latest = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.scenario_id == sc.id)
        .order_by(desc(PromptVersion.version))
        .limit(1)
    )
    latest = res_latest.scalars().first()
    next_ver = int(getattr(latest, "version", 0) or 0) + 1
    if pv.id:
        pv.status = "archived"
    db.add(
        PromptVersion(
            scenario_id=sc.id,
            version=next_ver,
            status="published",
            template_json=spec["template"],
            doc_refs_json=pv.doc_refs_json or spec.get("doc_refs") or [],
            params_json=pv.params_json,
            rollout_json=None,
            notes="auto: 主线任务微信/电话渠道分配（暂无电话主数据版）",
            published_at=datetime.now(),
        )
    )
    logger.info("Prompt seed: task_allocation v{} published（含 contact_channel 渠道说明）", next_ver)


async def seed_prompts_if_needed() -> None:
    """幂等 seed：仅在目标行缺失时写入。安全且快速，适合在启动阶段调用。"""
    try:
        async with AsyncSessionLocal() as db:
            for doc_key, name, filename in DOC_SEEDS:
                await _ensure_doc(db, doc_key, name, filename)
            for spec in SCENARIO_SEEDS:
                await _ensure_scenario(db, spec)
            await _ensure_task_allocation_doc_refs(db)
            await _ensure_task_allocation_channel_prompt(db)
            await db.commit()
        logger.info("Prompt seed: 完成")
    except Exception as e:
        logger.exception("Prompt seed 失败（不影响启动，稍后可重试）: {}", e)


def _main() -> None:
    asyncio.run(seed_prompts_if_needed())


if __name__ == "__main__":
    _main()
