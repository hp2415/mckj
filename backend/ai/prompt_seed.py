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

# 主对话场景文档字符预算（A0-1：避免全量 docx 淹没客户/聊天信号）
DOC_CHAR_LIMITS: dict[str, int] = {
    "ai_guide": 4000,
    "strategy": 6000,
    "opening": 3500,
    "closing": 4000,
    "phone": 8000,
}

# 场景 → 注入文档 key 列表（按顺序；已精简组合，见 OPTIMIZATION_PLAN A0-1）
SCENARIO_DOC_KEYS: dict[str, list[str]] = {
    "product_recommend": ["ai_guide", "strategy", "closing"],
    "general_chat": ["ai_guide", "strategy"],
    "staff_assistant": ["ai_guide", "strategy"],
}

# doc_key -> (name, filename)
# 与旧 doc_loader.DOC_FILES 保持同步，以免 seed 后丢文档。
# scoring_criteria（高意向/ABC 框架）请在「管理后台 → 提示词文档」维护 doc_key=scoring_criteria，勿用本地 docx 种子覆盖。
DOC_SEEDS: list[tuple[str, str, str]] = [
    ("ai_guide", "销售角色与行为规范", "AI聊天助手指引.docx"),
    ("opening", "开场破冰话术参考", "一、开场破冰.docx"),
    ("strategy", "客户分层话术参考", "2、各标签策略（含203040对应话术）.docx"),
    ("closing", "促成成交话术参考", "五、促成成交.docx"),
    ("regional_quotation", "常用区域报价整理", "regional_quotation.md"),
    # 单位性质×日历跟进策略；与客户动态标签无关，管理台可单独改文发布
    ("unit_followup_playbook", "单位性质跟进策略手册（非标签）", "unit_followup_playbook.md"),
    # 各团队 100~500 档人工方案的选品规律，注入方案选品场景
    ("proposal_playbook", "方案比选参考（人工方案选品规律）", "proposal_playbook.md"),
]

# 方案选品注入用：人工优秀方案的档位构成规律
_PROPOSAL_PLAYBOOK_DOC_REF: dict = {
    "doc_key": "proposal_playbook",
    "title": "方案比选参考（人工方案选品规律；key=proposal_playbook）",
    "required": False,
    "max_chars": 6000,
}

# 任务/画像注入用：单位跟进策略文档（与 profile_tags_detail 并列、勿混用）
_UNIT_FOLLOWUP_DOC_REF: dict = {
    "doc_key": "unit_followup_playbook",
    "title": "单位性质跟进策略手册（非客户动态标签；key=unit_followup_playbook）",
    "required": False,
    "max_chars": 12000,
}


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
3. 打招呼统一「称呼 + 好」（如「王老师好」）；禁止早上好/上午好/下午好/晚上好等时段问候
4. 禁止提及具体节气（大暑、立秋等）；季节寒暄不是必写；寒暄不要引用订单或聊天记录，见下方时间规则
{{time_context}}
"""

PROPOSAL_INTAKE_SYSTEM = """你是方案需求解析器。销售会用很随意的说法描述方案需求或调整上一版方案，请解析成结构化参数。

输入 JSON 含：
- text：本轮销售原话
- known：上一轮已确认的值（修订时务必尊重）
- prior_lines：上一版方案商品摘要，含 name 与 qty_per_person（每人件数）
- regex_hint：程序对「人均/人数」的粗提取，仅供参考，与 text 语义冲突时以 text 为准

## 字段
- per_capita_budget：每人/每份的预算金额（元）
- headcount：人数或份数；没提且 known 也没有时填 1（默认一份）
- discount_rate：折扣小数（九折=0.9，八八折=0.88）；仅当销售明确要求折扣时填写，没提给 null
- gross_margin：毛利率小数（25%=0.25，30%=0.30）；仅当销售明确要求改毛利率时填写，没提给 null
- include_keywords：点名要的品类短词（商品名里会出现的字）
- exclude_keywords：点名不要的品类短词
- shop_keywords：点名的店铺/产地；取消限制时返回 []
- item_kinds：商品种类数；没提给 null

## 硬规则（优先级最高）
1. 「每人 N 件」「改为每人 1 件」只描述件数，绝不是 per_capita_budget，也不是 item_kinds。
   若 text 只在改件数/换品类，per_capita_budget 与 headcount 必须原样返回 known。
2. 没出现「种」「样」「几种」「多少种」时，item_kinds 必须为 null（不要把「1件」当成 1 种）。
3. 「不局限某店」「某某的都可以」「放开店铺」→ shop_keywords=[]。
4. 品类数组返回「本轮之后应生效的完整列表」：known=["米","油"] 且 text「换成干货/加上干货」
   → ["米","油","干货"]；text「不要油了」→ 去掉油。
5. known 里已有的字段，text 没有明确要求改就照原样返回；不要猜、不要编造。
6. 未提人数/份数时：known 有则沿用；known 也没有则 headcount=1（默认一份），不要为此追问。
7. 「毛利率改为25」「改成25%毛利」「毛利率调到0.25」→ gross_margin=0.25；没提毛利率时必须 null。

## 简写
- 「60X5」「60*5」「60/5」通常是人均预算×人数（大的是预算，小的是人数）。
- 「人均300，10人份」「300元档10人」→ budget=300, headcount=10。
- 「人均300的方案」「出个200元档米油方案」→ 只给了预算时 headcount=1。

## 示例
text=将方案中每人3件的大米改为每人1件，剩下的预算换成干货
known={per_capita_budget:300,headcount:10,include_keywords:["米","油"],shop_keywords:["行唐"]}
prior_lines=[{name:行唐大米,qty_per_person:3},{name:行唐大豆油,qty_per_person:1}]
→ {"per_capita_budget":300,"headcount":10,"discount_rate":null,"gross_margin":null,"include_keywords":["米","油","干货"],"exclude_keywords":[],"shop_keywords":["行唐"],"item_kinds":null}

text=不局限于行唐县商铺，河北的都可以
known 同上
→ shop_keywords=[]，其余 known 不变

text=人均改成200
known={per_capita_budget:300,headcount:10}
→ {"per_capita_budget":200,"headcount":10,...,"item_kinds":null,"gross_margin":null}

text=把毛利率改为25
known={per_capita_budget:300,headcount:1}
→ {"per_capita_budget":300,"headcount":1,"discount_rate":null,"gross_margin":0.25,...,"item_kinds":null}

text=给我出一份人均300的北川米油方案
known={}
→ {"per_capita_budget":300,"headcount":1,...,"item_kinds":null,"gross_margin":null}

text=商品数量改为2种
→ item_kinds=2，预算人数沿用 known，gross_margin=null

## 输出
只输出 JSON，不要解释、不要 Markdown：
{"per_capita_budget":数字或null,"headcount":整数或null,"discount_rate":数字或null,"gross_margin":数字或null,
"include_keywords":[],"exclude_keywords":[],"shop_keywords":[],"item_kinds":整数或null}
"""

PROPOSAL_COMPOSE_SYSTEM = """你是脱贫地区农副产品（832平台）方案选品专家。
销售会给出人均预算、人数/份数和口头要求，你要从候选商品里组出一份可直接报价的方案。

## 硬性要求
1. 只能使用候选清单（candidates）里的商品，禁止编造商品、规格或价格。
2. 「单份」= 一个人/一份拿到的组合。所有商品的「优惠单价 × 每人数量」之和必须落在人均预算的 ±{{budget_tolerance_pct}}% 内，
   这是最重要的指标：宁可多选几件中小规格商品凑够预算，也不要只选一两件贵货把人均撑到预算的几倍。
3. requirements 是销售点名的要求（include_keywords 必须有、exclude_keywords 必须没有、
   shop_keywords 限定店铺/产地、item_kinds 限定商品种类数），优先级高于下面的默认偏好。
4. 默认一份 {{item_kinds_min}}-{{item_kinds_max}} 种商品（requirements.item_kinds 有值时以它为准）；每种商品每人 1 件为主，
   米面油等日常刚需可给到 2-{{max_qty_per_person}} 件——用件数而不是加品类去凑满预算。
5. 品类尽量分散（粮油、干货菌菇、肉蛋水产、茶饮、干果零食等），避免同一类目重复堆叠。
6. 商品尽量选取同一店铺（candidates 里的 shop / shop_id）；该店凑不齐再向外扩张。
7. 默认按成本价与毛利率计价：优惠单价 = 成本价 ÷ (1 − 毛利率)，默认毛利率 {{default_gross_margin_pct}}%
   （params.default_gross_margin={{default_gross_margin}}，可在提示词参数中调整）。
   候选里没有成本价的商品，程序已按平台价 {{fallback_discount_zhe}} 折算出 promo_price（兜底折扣
   fallback_discount_rate={{fallback_discount_rate}}，可在提示词参数中调整）。
   销售在对话里明确要求折扣时，改按「平台价 × 折扣」计价。价格一律以候选里的 promo_price 为准，禁止自行改价。

## 需求理解
- request 是销售最初的需求，feedback_history 是历次调整要求（越靠后越新），两者冲突时以最新的为准。
- 销售点名要的品类（如"除了油还需要米"）必须出现在方案里；点名不要的必须剔除。
- 只被要求「米油」这类少数品类时，不要自行加茶、腊肉等没被提到的品类去凑预算。
- 【参考修订】prior_lines 非空时是修订参考，不是强制清单：结合本轮预算与反馈，由你评估哪些保留、
  哪些换成更大/更合适规格，或换成同店其他商品；不必强行沿用全部 product_id。
  预算上调时优先加大规格、提高合适件数或换更优商品把人均凑近新预算；禁止为凑预算重复堆叠同款/同品类。
  主题与店铺约束仍以 requirements 为准；销售点名剔除的必须去掉。
- 销售改了人均预算或人数时，用新数值填 per_capita_budget / headcount；没提就沿用 constraints 里的值。
- 有 customer_context 时结合单位类型、历史采购与预算习惯选品：食堂采购偏大规格粮油米面，
  工会慰问偏礼盒与多品类组合。

## 输出
只输出 JSON，不要解释、不要 Markdown、不要代码块：
{"per_capita_budget":数字,"headcount":整数,"items":[{"product_id":整数,"qty_per_person":整数,"selling_point":"一句话卖点"}],"rationale":"一句话选品理由"}
items 的顺序即报价表的行顺序。
每条商品必须写 selling_point：给客户看的一句话卖点（约 8-20 字），突出品质、口感、产地、工艺或用途卖点；
可参考商品名与产地，例如「非转基因」「米香味足，粒粒分明」「东北黑土地长粒香」。
禁止写店铺名、价格、折扣、毛利率、规格复述或空话套话。
"""

PROPOSAL_GENERATE_SYSTEM = """你是农副产品方案生成调度助手。
本场景最终产物由后端异步生成 Excel；商品只能来自商品库，价格由程序按成本价与毛利率计算。
用户必须给出人均预算；人数/份数未给时默认按 1 份生成，不必追问份数。
仅当缺少人均预算时才追问。
有客户上下文时参考画像、预算和近期对话；无客户时只按用户明确要求。
不要编造商品、价格、文件地址，也不要输出 Markdown 报价表。
"""


CUSTOMER_PROFILE_SYSTEM = """你是一个专业的数据分析助手，请严格输出 JSON。
{{doc_block}}
若注入了「单位性质跟进策略手册」（unit_followup_playbook），按其中单位×日历规则填写跟进相关字段；该手册不是客户动态标签，勿与 matched_profile_tag_ids 混淆。
【可匹配的客户动态标签】
{{profile_tags_catalog}}
动态标签：非互斥项证据充分时可多选；目录末尾互斥组每组最多 1 个 id（联系频率档、新/老客户、男/女），拿不准则该组不打。严禁同时打上新客户和老客户。
"""

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
请结合基础信息、聊天记录与订单，判断客户符合上表中哪些标签。非互斥标签证据充分时可多选并写入 matched_profile_tag_ids；目录末尾互斥组每组最多 1 个 id，拿不准则该组不打。未匹配时输出空数组 []。不要给客户打上“📌 手动导入跟进”标签。
ai_profile分析时注意甄别基础信息、聊天记录与订单的发生时间，今年的信息为主，其他日期的为辅，不能时间线错乱。

请严格按以下要求提取并分析字段，并以 JSON 格式输出。
注意：
- contact_name: 请务必分析出“真实姓名”。不要直接使用微信昵称(name)。如果聊天或订单收货人提到“王老师”、“张局”等，提取姓氏或全名。
- contact_tel: 必须是纯数字字符串。若有多个电话，请用英文逗号“,”分隔。
- 无法推断的字段请留空。
- 综合订单中的购买产品，判断采购偏好和周期。
- purchase_months: 采购月份 (如: 1月,10月)；多个之间仅用英文逗号分隔，不要用顿号「、」或中文逗号；若是区间，请列出所有月份。
- entity_type: 只能输出一个最符合的单位类型。必须从以下类别中选择：[学校、消防、税务、街道办、人民政府、公检法、卫健委、银行、气象局、海关]。**单位性质跟进节奏**见 system 注入的「单位性质跟进策略手册」（`unit_followup_playbook`）；该手册**不是**动态标签，勿写入 `matched_profile_tag_ids`。
【高意向客户行为特征与ABC分级判定框架】
{{scoring_criteria}}
- ai_profile: 仅针对**客户本人**做销售视角客情分析：性格、沟通习惯、需求痛点、成交推进建议、约定事件等，注意信息年份，不要将非今年的信息拿到现在用，并分析其意向程度根据《高意向客户行为特征与ABC分级判定框架》为客户打分用于任务分配模型进行任务分配，如果订单中有近3天的订单数据则加上`近期已完成`，不超过100字。**禁止**在 ai_profile 中写入当前业务/销售微信号的名称、昵称、别名或「销售微信备注」等；此类信息由系统在对话时从数据库单独注入，与本 JSON 输出无关。单位性质相关节点（如学校学期采购）按「单位性质跟进策略手册」写一两句，勿与动态标签混淆。

- suggested_followup_date: **采购客户必填**；工作人员/内部同事/不负责采购等角色输出空字符串 `""`，且不要写【下一步跟进】块。
  1. 若客户有明确采购月份（如每年 10 月采购），建议在采购前 1-2 个月跟进
  2. 若客户回复积极、有近期需求意向，建议在 1-2 周内跟进
  3. 若客户较冷淡或长期未回复，建议在 1 个月后跟进
  4. 若信息不足无法精确推断，给出保守日期（默认取当前日期起约 1 个月后），**禁止留空**（非采购角色除外，须输出 `""`）
  5. **单位性质节奏**：若手册对当前 `entity_type` 有日历规则（如学校寒暑假），按手册填写跟进日；客户明确近期采购或已约定回访时以约定为准
- followup_strategy: **采购客户必填**，一句话、可直接执行，≤120 字；非采购角色输出 `""`。内容对齐「单位性质跟进策略手册」中该单位章节，勿照搬标签话术。
- followup_channel: **采购客户必填**，仅 `wechat` 或 `phone`；非采购角色输出 `""`。
- followup_reason: **采购客户必填**，≤80 字；非采购角色输出 `""`。
- callback_at / callback_note: **当客户明确约定「再联系时间」时填写**（含当天稍后、明天、后天、下周一、下周某天、某月某日等；如「等我开完会再联系」「下午再找我」「下周一再打给我」「周五下午联系」）；否则均输出 `""`。
  1. `callback_at` 格式 `YYYY-MM-DD HH:MM`；须结合下方「当前日期」（含星期）把相对说法换算成**具体日历日**，仅接受**当天起 90 天内**（含当天）；过去日期输出 `""`
  2. 能抽到具体钟点则用该钟点；仅有模糊时段时用默认：上午→10:00、下午→15:00、晚上→19:30、开完会/稍后/一会（且未指明改天）→取当前时刻起约 +1 小时（整点或半点）；仅说「下周一」等未指时段→默认 10:00
  3. `callback_note` ≤40 字，写客户原话或约定情境摘要；无约定则 `""`
  4. 若已填写 `callback_at`，`suggested_followup_date` 须与约定日期同一天

输出 JSON 字段：
1. contact_tel: 联系电话 (多个以逗号隔开)
2. contact_name: 联系人真实姓名
3. contact_title: 联系人职级/称呼 (如: 处长, 老师, 经理)
4. entity_name: 所属单位名称
5. entity_type: 单位性质
6. budget: 预算金额 (数字，有区间选择最大值)
7. purchase_months: 采购月份 (如: 1月,10月)，仅英文逗号分隔；按「单位性质跟进策略手册」与订单实绩填写（学校常见开学季相关月）
8. purchase_type: 采购类型 (食堂, 工会, 食堂+工会, 其它)
9. ai_profile: 仅客户客情画像 (性格、痛点、成交建议)；勿含销售/业务微信号信息
10. region_info: 详细地区信息 (省市县)
11. suggested_followup_date: 建议跟进日期 (YYYY-MM-DD)，采购客户必填；工作人员/内部同事/不负责等输出 `""`
12. matched_profile_tag_ids: 整数数组，元素必须为上方「可匹配的客户动态标签」中已列出的 id；非互斥标签可多选，互斥组每组最多 1 个（以目录末尾 id 为准）；无匹配则 []。不要打“📌 手动导入跟进”。客户信息中的 gender 字段 1 表示男、2 表示女。仔细判断对方是客户还是工作人员，给工作人员打上对应标签。新客户和老客户只能打其中一个。
13. abc_grade: 根据《高意向客户行为特征与ABC分级判定框架》输出单字母 A、B 或 C（必填其一，勿输出空字符串）
14. followup_strategy: 采购客户必填，≤120 字；非采购角色输出 `""`
15. followup_channel: 采购客户必填，`wechat` 或 `phone`；非采购角色输出 `""`
16. followup_reason: 采购客户必填，≤80 字；非采购角色输出 `""`
17. callback_at: 约定再联系时刻 (YYYY-MM-DD HH:MM)；无明确约定输出 `""`
18. callback_note: 约定摘要，≤40 字；无则 `""`

## 当前日期
{{current_date}}
"""

TASK_ALLOCATION_SYSTEM = """你是销售跟进任务编排助手，负责在「单个销售微信号」名下的一批已分析客户中，产出**本周期可执行的联系任务清单**。
{{doc_block}}
## 评分与分级（必读）
- 若已注入 `scoring_criteria`（ABC 框架），是判定意向层级、优先级与紧迫度的**首要依据**。
- 客户快照 `ai_profile` 中的评分叙述须与 ABC 框架**对照校验**。
- `strategy` 等话术文档仅作沟通补充，**不替代** ABC 在「谁优先」上的裁决。

## 主线任务渠道（必读）
主线任务须分为两类触达渠道，每条任务**必须**指定 `contact_channel`：
- **`wechat`（微信任务）**：通过微信私聊触达。`instruction` 为销售的具体执行任务描述（≤120 字）。
- **`phone`（电话任务）**：通过电话深沟通。`instruction` 为通话目标、开场白、需确认或推进的关键信息（≤120 字），**不要**写成微信可复制话术。

**电话任务选人（系统已接入电话外呼与微信语音触达明细）**：
- 本批数量由你在**下限与上限之间**自行决定：微信 **{{wechat_floor}}～{{wechat_cap}}**、电话 **{{phone_floor}}～{{phone_cap}}**，合计 **{{task_floor}}～{{task_cap}}**；**不可超出上限**。
- **宁缺毋滥**：合格客户不足时可以低于建议下限，**禁止**为凑满数量塞入不符合跟进日期、标签节奏或其他推荐条件的客户。
- `phone_cap`>0 且任务≥2 时，尽量两类都有，但不要为凑渠道比例硬改不合适的客户。
- 电话约占 `phone_cap/(wechat_cap+phone_cap)` 比例，优先选 **ABC 高意向（A/B 级）**、高预算、促单/比价/决策关键期、`rule_priority_score` 高、`priority_band=high`、且 `contact_voice_summary` 显示近期可接通或偏好语音的**重要客户**；长期呼不通者降低电话优先级，其余日常跟进用微信。
- 客户快照含 `phone` / `has_phone` / `phone_normalized`（合并销售好友绑定电话与主档规范化号码）与 `contact_voice_summary`（含手机直拨 `mobile_call` 与微信语音）；**有电话号的客户优先作为电话任务候选人**。`has_phone=false` 时不作为电话任务候选人。

本批渠道数量区间（上限硬约束，下限为建议值）：
- 微信任务：**{{wechat_floor}}** ≤ 条数 ≤ **{{wechat_cap}}**
- 电话任务：**{{phone_floor}}** ≤ 条数 ≤ **{{phone_cap}}**
- 合计：**{{task_floor}}** ≤ 条数 ≤ **{{task_cap}}**

渠道选择建议：日常跟进、报价确认、可即时互动的轻量触达 → 微信；重要客户深沟通、复杂决策链、需语音推进合作/回款 → **电话**；同一客户本批最多一条任务。

## 动态标签与联系节奏（必读；仅标签）
- 下方 user 中的 **「全量动态标签目录」** 与每条客户快照里的 **`profile_tags_detail`** 定义**客户状态/意向档位/联系频率**（如 20/30/40）。
- 系统已为每位客户计算 **`rule_priority_score`（0–100）**、**`tag_tier`（40/30/20 档位标签）**、**`priority_band`（high/mid/low）**；请优先采纳高分与 high 档，并结合 `days_since_last_main_task` 避免长期未排任务的客户再次被忽略。
- **不要**把全部客户都安排成「天天联系」；日任务仅在 cap 内选「今日该联系」者。
- 日任务（daily）：在渠道 cap 内，优先选出**今日到期应联系**的客户（结合标签策略 + `suggested_followup_date` + `recent_tasks` 上次联系/完成情况）。
- 周/月任务：在周期视野内做**分层排期**，`instruction` 可写明建议触达日或间隔，但不要求一次输出整周每一天的任务。
- 严格遵循前一天跳过详情，如果任务前一天跳过则今日不再进入任务队列

## 单位性质跟进策略（必读；非标签）
{{unit_season_context}}
- 注入文档 **「单位性质跟进策略手册」**（`unit_followup_playbook`）按单位性质×日历规定排期；与上方动态标签**完全独立**，禁止把「学校」等单位性质当成标签，也禁止用标签策略覆盖手册日历规则。
- 快照含 `unit_type`、`unit_segment`、`purchase_months`：识别学校等单位后，对照手册中对应章节 + 当前窗口码执行。

## 近期任务执行情况（必读）
- 每条客户快照含 `recent_tasks`（近若干日已分配任务的截止日、状态、标题等）。**昨日/前日已联系且状态为 done 的，除非标签策略要求每日触达且业务紧迫，否则今日通常不再入选。**
- `pending`/`overdue` 未完成的，应提高优先级或调整动作。
- `reserve` 储备任务，及未分配客户，不影响今日入选。
- `skipped` 跳过的，分析其跳过原因，除非表明联系时间或者业务紧迫，否则今日通常不再入选。

## 硬性要求
1. **只输出一个 JSON 对象**，不要 Markdown 围栏、不要前后解释。
2. `tasks` 中 `raw_customer_id` 必须与输入 JSON 完全一致；同一客户最多一条。
3. `tasks` 条数在建议区间内：合计 ≤ `{{task_cap}}`（建议 ≥ `{{task_floor}}`）；微信 ≤ `{{wechat_cap}}`、电话 ≤ `{{phone_cap}}`；合格客户不足时可低于下限，**禁止凑数**；`priority_rank` 从 1 递增。
4. `title` 简短；`instruction` 为可执行动作（不是话术且≤120 字），须与 `contact_channel` 匹配；**禁止**写成可复制发给客户的句子（如「XX好，好久没联系了」），问好与寒暄只属于客户话术，不写进任务 instruction。
5. `contact_channel`：**必填**，`wechat` | `phone`。
6. `task_kind`：`contact` | `follow_up` | `close_deal` | `revisit`（描述跟进目的，与渠道独立）。
7. `priority_score` 可选 0–100。
8. `rationale` 建议说明：微信/电话各几条、节奏分层思路、与标签策略及近期任务的取舍；若有学校客户取舍须点明是否因寒暑假/开学窗。
"""

TASK_ALLOCATION_USER = """
## 当前日期
{{current_date}}

## 分配上下文
- 销售业务微信号：{{sales_wechat_id}}
- 周期类型：{{period_type_label}}（{{period_type}}）
- 本周期：{{period_start}} 至 {{period_end}}
- 今日参考日：{{ref_today}}
- 本批任务数量区间：微信 **{{wechat_floor}}～{{wechat_cap}}** + 电话 **{{phone_floor}}～{{phone_cap}}** = 合计 **{{task_floor}}～{{task_cap}}**（上限不可超；宁缺毋滥）

## 当前单位业务窗口（非标签；细则见 system 注入的单位性质跟进策略手册）
{{unit_season_context}}

## 全量动态标签目录（仅标签：联系频率/意向档位；客户已打标签见各条 `profile_tags_detail`）
{{profile_tags_catalog}}

## 待分配客户（JSON；含 unit_type/unit_segment/purchase_months、phone/has_phone、ai_profile、profile_tags_detail、recent_tasks、contact_voice_summary）
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


TASK_ICEBREAKER_SYSTEM = """你是销售微信「客户激活」任务编排助手。输入客户均为：**近期互动变少**、**客户长期未回复**或**加好友后客户从未回复**的联系人（未必已有完整画像/评分；默认不含近期新加好友）。
{{doc_block}}
## 与主线任务的区别
- 主线任务侧重已建交、高意向、有画像评分的跟单；本批任务侧重**暖场、重新激活**，不要照搬「促单/比价」类高压动作。
- 若注入了 `opening` 开场话术、或 `scoring_criteria` / `strategy` 文档，可用来把握语气与节奏，但**仍以每条快照里的 icebreaker_reason、好友添加日、`last_customer_reply_date`（客户最近一次有效回复日）**为准；`last_chat_time` 可能含销售单向问候，勿当作客户已互动。参考文档若出现「上午好/下午好」或具体节气，生成时须改写为「XX好」，寒暄跟开场破冰参考走，勿照抄时段问候，也勿改成统一季节套话。

## 单位性质跟进策略（必读；非标签）
{{unit_season_context}}
- 激活排期同样遵守注入的 **「单位性质跟进策略手册」**（`unit_followup_playbook`）；与动态标签独立。深寒暑假学校客户少排激活，开学窗口可优先暖场，勿高压促单。

## 销售自称（撰写每条 `instruction` 时务必遵守）
{{sales_wechat_persona}}

### 自我介绍写法（强制）
- 对客户只说**口语化短自称**，优先「平台/单位 + 简称」，例如：`我是832平台的小张`。
- 从「对外昵称」提炼称呼：去掉账号前缀、渠道码、数字串、品牌堆叠；保留客户听得懂的姓/名/小名（如昵称「A脱贫832小张」→ 自称「小张」或「832平台的小张」）。
- **禁止**把微信号、wxid、括号账号、完整对外昵称原文整段贴进话术（反例：`我是832平台的A脱贫832小张（微信号AAfupin832）`）。
- **禁止**臆造与主数据无关的姓名；员工实名仅作内部核对，默认不写入客户话术，除非对外昵称缺失且必须署名。
- `instruction` 须可直接复制发送：一句称呼问好 + 一句短自我介绍 + 一句轻量寒暄/确认，避免一上来推品压单。
- 称呼问好统一「XX好」（如「王老师好」）；**禁止**早上好/上午好/下午好/晚上好等时段问候。
- **禁止**写具体节气（大暑、立秋等）。**季节寒暄不是必写**；寒暄以注入的开场破冰参考为准，轻量自然即可，不要套固定天气句。寒暄不要引用订单或聊天记录。

## 硬性要求
1. **只输出一个 JSON 对象**，不要 Markdown 围栏、不要前后解释。
2. `tasks` 中每条 `raw_customer_id` 必须与输入 JSON 完全一致；每条 `task_kind` **必须为** `icebreaker`。
3. 同一 `raw_customer_id` 最多一条；条数不得超过 `{{task_cap}}`。
4. `title` **必须以「激活 · 」开头**（后接简短描述，勿使用「破冰」字样）；`instruction` 为销售**可直接复制发送**的微信话术；自我介绍须遵守上方「自我介绍写法」，勿照搬微信号或冗长昵称。
5. `priority_score` 可选（0–100），表示今日激活触达的紧迫度；越久未互动可略高。
6. **输入客户列表非空时，须从中选出至多 `{{task_cap}}` 条生成 tasks**；仅当某条 `recent_tasks` 明确显示**昨日已完成**或**今日已有 outbound** 时才跳过该客户。深寒暑假须**优先非学校**；候选几乎全是学校时可少于 cap，**禁止为凑满 cap 用学校客户充数**。
7. 每条快照含 `recent_tasks`：仅作单客户去重参考，勿据此否定整批候选。
{{time_context}}
"""

TASK_ICEBREAKER_USER = """
## 当前日期
{{current_date}}

## 销售员身份（员工实名与业务微信主数据）
{{staff_identity}}

## 本销售对客户的自称（只取口语短称呼写入 instruction；微信号勿写入话术）
{{sales_wechat_persona}}

## 上下文
- 销售业务微信号：{{sales_wechat_id}}（仅内部标识，禁止写入发给客户的 instruction）
- 今日参考日：{{ref_today}}
- 当前季节：{{season_label}}（仅防季节说反；勿写节气名，勿写上午好/下午好；季节寒暄不是必写）
- 本批任务上限：{{task_cap}}
- 说明：下列客户已按规则筛为「近期互动变少（约 {{ice_lapsed_days}} 日未回复）」或「客户长期未回复（约 ≥{{ice_stale_days}} 天，以有效聊天为准）」或「加好友较早但客户从未回复」（不含近期新加好友）。

## 当前单位业务窗口（非标签；细则见单位性质跟进策略手册）
{{unit_season_context}}

## 待生成激活任务的客户快照
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
      "title": "激活 · 简短标题",
      "instruction": "今日微信侧具体激活话术",
      "task_kind": "icebreaker"
    }
  ],
  "rationale": "可选"
}

若输入 JSON 数组为空：{"tasks": [], "rationale": "无符合条件的激活客户"}。
若数组非空：优先非学校；深寒暑假学校默认不排，条数可少于 {{task_cap}}。
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
2. 常规商品（商品库内）需要查价、查库存时，可使用 search_products 检索工具。
3. 现采、外部采买不在商品库中。用户提到「现采」「外部采买」或区域报价时，**必须**调用 lookup_regional_quotation 工具查询，不要声称文档未提供、也不要用 search_products。
4. 若问题依赖某位客户的订单、画像或微信记录，请明确告知用户切换到「客户对话」并在左侧选择该客户后再问。
5. 回复简洁、可执行；短句分段，避免大段 Markdown。
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
3. 打招呼统一「称呼 + 好」（如王老师好）；禁止上午好/下午好等时段问候；禁止具体节气名；季节寒暄不是必写；寒暄不要引用订单或聊天记录
{{time_context}}
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


# 与 doc_loader / A0-1 预算一致的 doc_refs 工厂
def _doc_ref(doc_key: str, title: str, *, max_chars: int | None = None) -> dict:
    limit = max_chars if max_chars is not None else DOC_CHAR_LIMITS.get(doc_key)
    return {"doc_key": doc_key, "title": title, "required": False, "max_chars": limit}


# 与旧 doc_loader.get_docs_for_scenario 保持一致的注入顺序与取舍（A0-1 已加 max_chars 并精简组合）：
# - product_recommend: ai_guide + strategy + closing
# - general_chat:      ai_guide + strategy（ongoing 客户沟通不需开场破冰全文）
# - staff_assistant:   ai_guide + strategy（内部问答不需 opening）
SCENARIO_SEEDS: list[dict] = [
    {
        "scenario_key": "proposal_intake",
        "name": "方案需求解析",
        "description": "后端小模型：把销售的口语需求解析成人均预算、人数/份数与可选折扣。",
        "ui_category": "backend_only",
        "template": {
            "system": PROPOSAL_INTAKE_SYSTEM,
            "notes": "口语写法识别规则请在此维护，避免在代码里堆关键词",
        },
        "doc_refs": [],
        "tools_enabled": False,
        "params": {"temperature": 0.0, "max_tokens": 200},
    },
    {
        "scenario_key": "proposal_compose",
        "name": "方案选品编排",
        "description": "后端异步管线：按人均预算与销售要求从商品库选品，输出 JSON 供程序算价出表。",
        "ui_category": "backend_only",
        "template": {
            "system": PROPOSAL_COMPOSE_SYSTEM,
            "notes": "选品规则请在此维护；程序只做算价、校验与 Excel 渲染",
        },
        "doc_refs": [_PROPOSAL_PLAYBOOK_DOC_REF],
        "tools_enabled": False,
        "params": {
            "temperature": 0.2,
            "max_tokens": 900,
            # 以下为方案效果策略，可在提示词管理里直接改，无需改代码
            # 默认毛利率 30%：优惠单价 = 成本价 ÷ (1 − 毛利率)
            "default_gross_margin": 0.30,
            # 无成本价时按平台价八八折兜底
            "fallback_discount_rate": 0.88,
            "budget_tolerance": 0.08,
            "item_kinds_min": 3,
            "item_kinds_max": 6,
            "max_qty_per_person": 6,
            "max_lines": 8,
        },
    },
    {
        "scenario_key": "proposal_generate",
        "name": "方案生成",
        "description": "客户对话：异步生成可预览/下载的 Excel 报价供应表（人均/人份方案）。非朋友圈文案、推广文案或营销文案。",
        "ui_category": "customer_chat",
        "template": {"system": PROPOSAL_GENERATE_SYSTEM, "notes": "方案生成由 gateway 入队异步管线"},
        "doc_refs": [],
        "tools_enabled": False,
        "router_hints": {
            "keywords": ["人份", "人均", "工会方案", "报价表", "供应表", "方案表", "元档"],
            "examples": [
                "根据客户偏好出具一份人均200、10人份的方案",
                "帮这个客户做一个300元档、20人的工会慰问方案",
                "按画像出一版包邮米油组合方案",
            ],
            "anti_keywords": [
                "朋友圈",
                "文案",
                "海报",
                "宣传语",
                "短视频",
                "电话话术",
                "开场白怎么写",
            ],
            "anti_examples": [
                "写一条朋友圈文案",
                "帮我写推广文案",
                "出一份朋友圈文案方案",
                "做一版活动宣传文案",
            ],
            "requires_customer": True,
            "priority": 15,
        },
    },
    {
        "scenario_key": "proposal_generate_free",
        "name": "方案生成（自由对话）",
        "description": "自由对话：异步生成可预览/下载的 Excel 报价供应表（人均/人份方案）。非朋友圈文案、推广文案或营销文案。",
        "ui_category": "free_chat",
        "template": {"system": PROPOSAL_GENERATE_SYSTEM, "notes": "自由方案生成由 gateway 入队异步管线"},
        "doc_refs": [],
        "tools_enabled": False,
        "router_hints": {
            "keywords": ["人份", "人均", "工会方案", "报价表", "供应表", "方案表", "元档"],
            "examples": [
                "按人均150做一版10人米油方案",
                "做个工会300档50人慰问方案",
                "按九折出人均200的10人方案",
            ],
            "anti_keywords": [
                "朋友圈",
                "文案",
                "海报",
                "宣传语",
                "短视频",
                "电话话术",
                "开场白怎么写",
            ],
            "anti_examples": [
                "写一条朋友圈文案",
                "帮我写推广文案",
                "出一份朋友圈文案方案",
                "做一版活动宣传文案",
            ],
            "requires_customer": False,
            "priority": 15,
        },
    },
    {
        "scenario_key": "product_recommend",
        "name": "推品报价",
        "description": "帮助销售人员为客户推荐合适商品并生成可直接发送的微信话术。",
        "ui_category": "customer_chat",
        "template": {"system": PRODUCT_RECOMMEND_SYSTEM, "notes": "迁移自 prompts.get_product_recommend_prompt"},
        "doc_refs": [
            _doc_ref("ai_guide", "销售角色与行为规范"),
            _doc_ref("strategy", "客户分层话术参考"),
            _doc_ref("closing", "促成成交话术参考"),
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
            _doc_ref("ai_guide", "销售角色与行为规范"),
            _doc_ref("strategy", "客户分层话术参考"),
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
            _doc_ref("regional_quotation", "常用区域报价整理", max_chars=None),
            _doc_ref("ai_guide", "销售角色与行为规范"),
            _doc_ref("strategy", "客户分层话术参考", max_chars=5000),
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
        "description": "原始客户池 LLM 画像：根据基础信息、聊天记录、订单历史、近期联系任务与申诉反馈输出结构化 JSON。",
        "ui_category": "backend_only",
        "template": {
            "system": CUSTOMER_PROFILE_SYSTEM.strip(),
            "user": CUSTOMER_PROFILE_USER.strip(),
            "notes": "迁移自 ai.raw_profiling.PROMPT_TEMPLATE；system 为 JSON 约束，user 为任务与上下文；可注入单位性质跟进策略手册（非标签）。",
        },
        "doc_refs": [
            dict(_UNIT_FOLLOWUP_DOC_REF),
        ],
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
                "title": "客户动态标签说明（仅标签；profile_tags_detail）",
                "required": False,
                "max_chars": 12000,
            },
            dict(_UNIT_FOLLOWUP_DOC_REF),
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
        "name": "销售激活任务分配（日）",
        "description": "后台：日任务补充——长期未聊等客户的激活任务 JSON（默认不含新加好友）；与 task_allocation 并行第二条 LLM。",
        "ui_category": "backend_only",
        "template": {
            "system": TASK_ICEBREAKER_SYSTEM,
            "user": TASK_ICEBREAKER_USER.strip(),
            "notes": "激活专用；注入 opening、单位性质跟进策略手册（非标签）、scoring_criteria、strategy。",
        },
        "doc_refs": [
            {"doc_key": "opening", "title": "开场破冰话术参考", "required": False, "max_chars": 8000},
            dict(_UNIT_FOLLOWUP_DOC_REF),
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
            _doc_ref("phone", "首通电话不同场景话术"),
            _doc_ref("ai_guide", "销售角色与行为规范", max_chars=3000),
        ],
        "tools_enabled": False,
        "router_hints": {
            "examples": ["（电话工作台专用，由客户端指定 scenario，不参与对话路由）"],
            "requires_customer": True,
            "priority": 0,
        },
    },
]


def _read_seed_doc_text(filename: str) -> str:
    path = DATA_DIR / filename
    if not path.exists():
        logger.warning("Prompt seed: 话术文档不存在，跳过: {}", path)
        return ""
    if filename.lower().endswith(".md"):
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.error("Prompt seed: 读取 md 失败 {}: {}", path.name, e)
            return ""
    return _read_docx_text(filename)


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
        content = _read_seed_doc_text(filename)
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
    elif doc_key in ("regional_quotation", "unit_followup_playbook") and not (ver.content or "").strip():
        # 运营可能已建 doc 但正文为空：用本地 md 回填 published 版本
        content = _read_seed_doc_text(filename)
        if content:
            if ver.status == "published":
                ver.status = "archived"
            res_max = await db.execute(
                select(PromptDocVersion.version)
                .where(PromptDocVersion.doc_id == doc.id)
                .order_by(desc(PromptDocVersion.version))
                .limit(1)
            )
            next_v = int(res_max.scalar() or 0) + 1
            db.add(PromptDocVersion(
                doc_id=doc.id,
                version=next_v,
                status="published",
                content=content,
                source_filename=filename,
                published_at=datetime.now(),
            ))
            await db.flush()
            logger.info(
                "Prompt seed: 话术文档 {} v{} published 已从 {} 回填 ({} 字符)",
                doc_key, next_v, filename, len(content),
            )
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
            params_json=spec.get("params") or None,
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
                "title": "客户动态标签说明（仅标签；profile_tags_detail）",
                "required": False,
                "max_chars": 12000,
            }
        )
    if "unit_followup_playbook" not in keys:
        prepend.append(dict(_UNIT_FOLLOWUP_DOC_REF))
    if prepend:
        pv.doc_refs_json = prepend + refs
        logger.info(
            "Prompt seed: task_allocation 已发布版本已补全 doc_refs: {}",
            [p["doc_key"] for p in prepend],
        )


async def _ensure_scenario_doc_ref(
    db,
    *,
    scenario_key: str,
    doc_ref: dict,
) -> None:
    """为已发布场景版本补全单个 doc_ref（幂等）。"""
    key = str(doc_ref.get("doc_key") or "")
    if not key:
        return
    res = await db.execute(select(PromptScenario).where(PromptScenario.scenario_key == scenario_key))
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
        refs = []
    keys = {str((r or {}).get("doc_key") or "") for r in refs if isinstance(r, dict)}
    if key in keys:
        return
    pv.doc_refs_json = [dict(doc_ref)] + list(refs)
    logger.info("Prompt seed: {} 已发布版本已补全 doc_ref={}", scenario_key, key)


async def _ensure_task_allocation_channel_prompt(db) -> None:
    """
    兼容旧库：task_allocation 已发布版本若无渠道区间/宁缺毋滥口径，自动发布新版本。
    """
    # 旧版无渠道说明时补全；marker 须在现行 seed 中仍存在，避免反复发布
    await _publish_scenario_seed_if_missing_marker(
        db,
        scenario_key="task_allocation",
        marker="主线任务渠道（必读）",
        notes="auto: 主线任务微信/电话渠道分配（含好友绑定+规范化电话）",
        check_field="system",
    )
    await _publish_scenario_seed_if_missing_marker(
        db,
        scenario_key="task_allocation",
        marker="宁缺毋滥",
        notes="auto: 主线数量改为下限~上限由模型决定，禁止规则式凑数",
        check_field="system",
    )


async def _ensure_proposal_intake_requirements(db) -> None:
    """兼容旧库：方案需求解析缺少品类/店铺/种类数字段时，自动发布新版本。

    这几个字段是「北川店铺的米油方案」「商品改为2种」能不能被选品环节看见的前提，
    旧版本只解析人均与人数，选品只能靠自由文本揣摩。
    """
    await _publish_scenario_seed_if_missing_marker(
        db,
        scenario_key="proposal_intake",
        marker="prior_lines",
        notes="auto: intake 以模型为主，注入 prior_lines 与 few-shot",
        check_field="system",
    )
    await _publish_scenario_seed_if_missing_marker(
        db,
        scenario_key="proposal_intake",
        marker="默认一份",
        notes="auto: 未提份数时默认 headcount=1",
        check_field="system",
    )
    await _publish_scenario_seed_if_missing_marker(
        db,
        scenario_key="proposal_intake",
        marker="gross_margin",
        notes="auto: intake 支持对话调整毛利率",
        check_field="system",
    )
    for scenario_key in ("proposal_generate", "proposal_generate_free"):
        await _publish_scenario_seed_if_missing_marker(
            db,
            scenario_key=scenario_key,
            marker="默认按 1 份",
            notes="auto: 方案生成仅必填人均，份数默认 1",
            check_field="system",
        )
        await _publish_scenario_seed_if_missing_marker(
            db,
            scenario_key=scenario_key,
            marker="成本价与毛利率",
            notes="auto: 方案计价改为成本价与毛利率",
            check_field="system",
        )
    # 选品场景引用人工方案比选规律；已发布版本只补 doc_ref，不覆盖运营改过的正文
    await _ensure_scenario_doc_ref(
        db, scenario_key="proposal_compose", doc_ref=_PROPOSAL_PLAYBOOK_DOC_REF
    )
    await _publish_scenario_seed_if_missing_marker(
        db,
        scenario_key="proposal_compose",
        marker="参考修订",
        notes="auto: 选品修订改为 prior 参考、由模型评估换货",
        check_field="system",
    )
    await _publish_scenario_seed_if_missing_marker(
        db,
        scenario_key="proposal_compose",
        marker="selling_point",
        notes="auto: 选品输出一句话卖点，写入方案表末列",
        check_field="system",
    )
    await _publish_scenario_seed_if_missing_marker(
        db,
        scenario_key="proposal_compose",
        marker="default_gross_margin",
        notes="auto: 选品默认按成本价与毛利率计价",
        check_field="system",
    )
    await _publish_scenario_seed_if_missing_marker(
        db,
        scenario_key="proposal_compose",
        marker="fallback_discount_rate",
        notes="auto: 无成本价时按平台价八八折兜底",
        check_field="system",
    )


_PROPOSAL_COPY_ANTI_EXAMPLES = (
    "写一条朋友圈文案",
    "帮我写推广文案",
    "出一份朋友圈文案方案",
    "做一版活动宣传文案",
)
_PROPOSAL_COPY_ANTI_KEYWORDS = (
    "朋友圈",
    "文案",
    "海报",
    "宣传语",
    "短视频",
    "电话话术",
    "开场白怎么写",
)


async def _ensure_proposal_router_anti_copywriting(db) -> None:
    """兼容旧库：方案场景补齐文案/朋友圈反例，避免路由小模型误分到 Excel 方案。"""
    for scenario_key in ("proposal_generate", "proposal_generate_free"):
        spec = next((s for s in SCENARIO_SEEDS if s["scenario_key"] == scenario_key), None)
        res = await db.execute(
            select(PromptScenario).where(PromptScenario.scenario_key == scenario_key)
        )
        sc = res.scalars().first()
        if not sc or not spec:
            continue
        hints = dict(sc.router_hints_json) if isinstance(sc.router_hints_json, dict) else {}
        changed = False

        anti_examples = [str(x) for x in (hints.get("anti_examples") or []) if str(x).strip()]
        for example in _PROPOSAL_COPY_ANTI_EXAMPLES:
            if example not in anti_examples:
                anti_examples.append(example)
                changed = True
        if anti_examples != list(hints.get("anti_examples") or []):
            hints["anti_examples"] = anti_examples
            changed = True

        anti_keywords = [str(x) for x in (hints.get("anti_keywords") or []) if str(x).strip()]
        for word in _PROPOSAL_COPY_ANTI_KEYWORDS:
            if word not in anti_keywords:
                anti_keywords.append(word)
                changed = True
        if anti_keywords != list(hints.get("anti_keywords") or []):
            hints["anti_keywords"] = anti_keywords
            changed = True

        seed_desc = str(spec.get("description") or "")
        if seed_desc and "非朋友圈文案" in seed_desc and sc.description != seed_desc:
            sc.description = seed_desc
            changed = True

        if changed:
            sc.router_hints_json = hints
            logger.info("Prompt seed: {} 已补齐文案/朋友圈路由反例", scenario_key)


async def _ensure_proposal_compose_policy(db) -> None:
    """兼容旧库：把效果策略参数写进 proposal_compose 的 params，并刷新模板占位符。

    默认折扣、预算容差、种类区间等以前写死在代码里；迁到提示词 params 后，
    运营可在管理后台直接改。已有 params 只补缺失键，不覆盖已改过的值。
    模板若仍是硬编码「±8%」，则发布带 {{budget_tolerance_pct}} 等占位符的新版本。
    """
    from ai.proposal.policy import POLICY_KEYS

    spec = next((s for s in SCENARIO_SEEDS if s["scenario_key"] == "proposal_compose"), None)
    if not spec:
        return
    res = await db.execute(select(PromptScenario).where(PromptScenario.scenario_key == "proposal_compose"))
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

    seed_params = dict(spec.get("params") or {})
    current = dict(pv.params_json) if isinstance(pv.params_json, dict) else {}
    merged = dict(current)
    params_changed = False
    for key in POLICY_KEYS:
        if key not in merged and key in seed_params:
            merged[key] = seed_params[key]
            params_changed = True

    tpl = pv.template_json if isinstance(pv.template_json, dict) else {}
    system = str(tpl.get("system") or "")
    needs_placeholders = (
        "{{budget_tolerance_pct}}" not in system
        or "{{default_gross_margin" not in system
        or "{{fallback_discount" not in system
    )

    if params_changed and not needs_placeholders:
        # 只补参数，保留运营改过的正文
        pv.params_json = merged
        logger.info("Prompt seed: proposal_compose 已补齐策略 params {}", list(POLICY_KEYS))
        return

    if not params_changed and not needs_placeholders:
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
    new_template = dict(spec["template"]) if needs_placeholders else dict(tpl)
    for key, value in seed_params.items():
        if key in ("temperature", "max_tokens", "model", "tools_enabled"):
            merged.setdefault(key, value)
    db.add(
        PromptVersion(
            scenario_id=sc.id,
            version=next_ver,
            status="published",
            template_json=new_template,
            doc_refs_json=pv.doc_refs_json or spec.get("doc_refs") or [],
            params_json=merged,
            rollout_json=None,
            notes="auto: 方案效果参数迁入提示词 params（毛利率/容差/种类/件数）",
            published_at=datetime.now(),
        )
    )
    logger.info("Prompt seed: proposal_compose v{} published（效果策略参数）", next_ver)


async def _publish_scenario_seed_if_missing_marker(
    db,
    *,
    scenario_key: str,
    marker: str,
    notes: str,
    check_field: str = "system",
) -> None:
    """已发布版本缺少 marker 时，用 SCENARIO_SEEDS 模板发布新版本（幂等）。"""
    spec = next((s for s in SCENARIO_SEEDS if s["scenario_key"] == scenario_key), None)
    if not spec:
        return
    res = await db.execute(select(PromptScenario).where(PromptScenario.scenario_key == scenario_key))
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
    if not isinstance(tpl, dict):
        tpl = {}
    check_text = str(tpl.get(check_field) or tpl.get("system") or tpl.get("user") or "")
    if marker in check_text:
        return
    # 同时扫 system+user，避免 marker 只在另一侧
    combined = f"{tpl.get('system') or ''}\n{tpl.get('user') or ''}"
    if marker in combined:
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
            notes=notes,
            published_at=datetime.now(),
        )
    )
    logger.info("Prompt seed: {} v{} published（{}）", scenario_key, next_ver, notes)


async def _ensure_optional_season_greeting_prompts(db) -> None:
    """兼容旧库：去掉硬编码季节寒暄例句；寒暄跟破冰参考，不引用订单/聊天。"""
    notes = "auto: 寒暄跟开场破冰参考，不引用订单或聊天"
    marker = "寒暄不要引用订单或聊天记录"
    for scenario_key in (
        "product_recommend",
        "general_chat",
        "task_allocation_icebreaker",
    ):
        await _publish_scenario_seed_if_missing_marker(
            db,
            scenario_key=scenario_key,
            marker=marker,
            notes=notes,
            check_field="system",
        )
    await _publish_scenario_seed_if_missing_marker(
        db,
        scenario_key="task_allocation",
        marker="问好与寒暄只属于客户话术",
        notes="auto: 主线 instruction 反例去掉季节套话",
        check_field="system",
    )


async def _ensure_unit_season_prompts(db) -> None:
    """兼容旧库：补齐单位性质跟进策略手册引用与相关提示词。"""
    await _ensure_scenario_doc_ref(
        db, scenario_key="task_allocation", doc_ref=_UNIT_FOLLOWUP_DOC_REF
    )
    await _ensure_scenario_doc_ref(
        db, scenario_key="task_allocation_icebreaker", doc_ref=_UNIT_FOLLOWUP_DOC_REF
    )
    await _ensure_scenario_doc_ref(
        db, scenario_key="customer_profile", doc_ref=_UNIT_FOLLOWUP_DOC_REF
    )
    await _publish_scenario_seed_if_missing_marker(
        db,
        scenario_key="task_allocation",
        marker="单位性质跟进策略（必读；非标签）",
        notes="auto: 单位性质跟进策略手册与标签分离",
        check_field="system",
    )
    await _publish_scenario_seed_if_missing_marker(
        db,
        scenario_key="task_allocation_icebreaker",
        marker="单位性质跟进策略（必读；非标签）",
        notes="auto: 激活任务引用单位性质跟进策略手册（非标签）",
        check_field="system",
    )
    await _publish_scenario_seed_if_missing_marker(
        db,
        scenario_key="customer_profile",
        marker="单位性质跟进策略手册",
        notes="auto: 画像引用单位性质跟进策略手册（非标签）",
        check_field="system",
    )


def _doc_refs_need_budget_update(current: list, target: list[dict]) -> bool:
    """已发布版本的 doc_refs 是否与 A0-1 目标不一致（缺 max_chars 或 doc 组合不同）。"""
    if not isinstance(current, list):
        return True
    cur_keys = [str((r or {}).get("doc_key") or "") for r in current if isinstance(r, dict)]
    tgt_keys = [str(r.get("doc_key") or "") for r in target]
    if cur_keys != tgt_keys:
        return True
    tgt_by_key = {str(r["doc_key"]): r for r in target}
    for item in current:
        if not isinstance(item, dict):
            return True
        key = str(item.get("doc_key") or "")
        tgt = tgt_by_key.get(key)
        if not tgt:
            return True
        cur_limit = item.get("max_chars")
        tgt_limit = tgt.get("max_chars")
        if cur_limit in (None, "", 0) and tgt_limit:
            return True
    return False


async def _ensure_main_chat_doc_budget(db) -> None:
    """
    兼容旧库：为主对话/电话场景已发布版本补全 doc max_chars，并按 A0-1 精简 doc 组合。
    仅更新 doc_refs_json，不改动 system 模板正文。
    """
    targets: dict[str, list[dict]] = {}
    for spec in SCENARIO_SEEDS:
        key = spec["scenario_key"]
        if key in (
            "product_recommend",
            "general_chat",
            "staff_assistant",
            "phone_call_script",
        ):
            targets[key] = list(spec.get("doc_refs") or [])

    for scenario_key, target_refs in targets.items():
        if not target_refs:
            continue
        res = await db.execute(
            select(PromptScenario).where(PromptScenario.scenario_key == scenario_key)
        )
        sc = res.scalars().first()
        if not sc:
            continue
        res_v = await db.execute(
            select(PromptVersion)
            .where(PromptVersion.scenario_id == sc.id)
            .where(PromptVersion.status == "published")
            .order_by(desc(PromptVersion.version))
            .limit(1)
        )
        pv = res_v.scalars().first()
        if not pv:
            continue
        refs = pv.doc_refs_json or []
        if not _doc_refs_need_budget_update(refs, target_refs):
            continue
        pv.doc_refs_json = target_refs
        logger.info(
            "Prompt seed: {} 已发布版本 doc_refs 已应用 A0-1 预算 keys={}",
            scenario_key,
            [r.get("doc_key") for r in target_refs],
        )


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
            await _ensure_proposal_intake_requirements(db)
            await _ensure_proposal_router_anti_copywriting(db)
            await _ensure_proposal_compose_policy(db)
            await _ensure_unit_season_prompts(db)
            await _ensure_optional_season_greeting_prompts(db)
            await _ensure_main_chat_doc_budget(db)
            from ai.profile_input_budget import ensure_profile_budget_config_defaults
            await ensure_profile_budget_config_defaults(db)
            await db.commit()
        from ai.prompt_store import get_prompt_store
        store = get_prompt_store()
        await store.invalidate_doc("regional_quotation")
        await store.invalidate_doc("unit_followup_playbook")
        for key in (
            "proposal_intake",
            "proposal_compose",
            "proposal_generate",
            "proposal_generate_free",
            "product_recommend",
            "general_chat",
            "staff_assistant",
            "phone_call_script",
            "task_allocation",
            "task_allocation_icebreaker",
            "customer_profile",
        ):
            await store.invalidate_scenario(key)
        logger.info("Prompt seed: 完成")
    except Exception as e:
        logger.exception("Prompt seed 失败（不影响启动，稍后可重试）: {}", e)


def _main() -> None:
    asyncio.run(seed_prompts_if_needed())


if __name__ == "__main__":
    _main()
