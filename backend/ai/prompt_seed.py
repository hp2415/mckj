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


DATA_DIR = Path(__file__).parent.parent / "data"

# doc_key -> (name, filename)
# 与旧 doc_loader.DOC_FILES 保持同步，以免 seed 后丢文档。
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

## 当前销售员身份（员工姓名）
{{staff_identity}}

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


STAFF_ASSISTANT_SYSTEM = """你是面向一线销售人员的内部业务助手。当前未在系统中锁定任何客户，请直接回答销售同事的问题。
{{doc_block}}
## 当前日期
{{current_date}}

## 当前销售员身份
{{staff_identity}}

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

## 当前销售员身份（员工姓名）
{{staff_identity}}

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
    if not sc:
        sc = PromptScenario(
            scenario_key=key,
            name=spec["name"],
            description=spec.get("description"),
            enabled=True,
            tools_enabled=bool(spec.get("tools_enabled", True)),
            ui_category=spec.get("ui_category", "customer_chat"),
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


async def seed_prompts_if_needed() -> None:
    """幂等 seed：仅在目标行缺失时写入。安全且快速，适合在启动阶段调用。"""
    try:
        async with AsyncSessionLocal() as db:
            for doc_key, name, filename in DOC_SEEDS:
                await _ensure_doc(db, doc_key, name, filename)
            for spec in SCENARIO_SEEDS:
                await _ensure_scenario(db, spec)
            await db.commit()
        logger.info("Prompt seed: 完成")
    except Exception as e:
        logger.exception("Prompt seed 失败（不影响启动，稍后可重试）: {}", e)


def _main() -> None:
    asyncio.run(seed_prompts_if_needed())


if __name__ == "__main__":
    _main()
