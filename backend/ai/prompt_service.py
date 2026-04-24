"""
PromptService：gateway 调用的统一入口。

职责：
1) 读取功能开关（SystemConfig prompt.use_db_prompts），开关关闭时回退到旧 prompts.py 的 get_prompt_for_scenario
   + doc_loader.get_docs_for_scenario，保障随时可回滚。
2) 开关打开时：PromptStore 取 published 版本 → 拉取引用的文档 → PromptRenderer 渲染 system →
   build_messages 组装成 OpenAI messages。
3) 输出 PromptResolution（含 meta，用于日志与审计）。
4) 预留 DecisionEngine（Phase3）钩子：tags 参数 + 规则命中后覆盖 version/params。

不负责：
- 客户上下文装配（由 ContextAssembler 完成）
- LLM 调用（由 LLMClient 完成）
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from core.logger import logger
from models import SystemConfig

from ai.prompt_models import (
    PromptResolution,
    PromptParams,
    PromptVersionView,
)
from ai.prompt_store import PromptStore, get_prompt_store
from ai.prompt_renderer import render_system, build_messages


FEATURE_FLAG_KEY = "use_db_prompts"
FEATURE_FLAG_GROUP = "prompt"


class PromptService:
    """提示词解析入口。

    典型使用：
        service = PromptService(db, store=get_prompt_store())
        resolution = await service.resolve(
            scenario_key="general_chat",
            ctx=ctx,
            query=query,
            history=ctx.get("ai_history_messages", []),
            customer_id=customer_id,
            user_id=user_id,
        )
        messages = resolution.messages
    """

    def __init__(self, db: AsyncSession, store: Optional[PromptStore] = None):
        self.db = db
        self.store = store or get_prompt_store()

    async def _use_db_prompts(self) -> bool:
        """读开关；未配置则默认 True（启用 DB 化提示词）。"""
        try:
            stmt = select(SystemConfig).where(SystemConfig.config_key == FEATURE_FLAG_KEY)
            res = await self.db.execute(stmt)
            cfg = res.scalars().first()
            if not cfg:
                return True
            return str(cfg.config_value).strip() not in ("0", "false", "False", "off", "OFF", "")
        except Exception as e:
            logger.warning("PromptService: 读取开关失败，默认启用 DB 提示词: {}", e)
            return True

    async def resolve(
        self,
        *,
        scenario_key: str,
        ctx: dict,
        query: str,
        history: list[dict] | None = None,
        customer_id: Optional[int] = None,
        user_id: Optional[int] = None,
        tags: Optional[dict] = None,
    ) -> PromptResolution:
        history = history or []
        use_db = await self._use_db_prompts()

        if not use_db:
            return await self._resolve_legacy(scenario_key, ctx, query, history)

        version = await self.store.get_published_version(scenario_key)
        if version is None:
            # 保底：DB 里没有该场景的 published 版本时，回退到老代码，避免线上中断
            logger.warning(
                "PromptService: DB 中未找到场景 '{}' 的 published 版本，回退到旧 prompts.py",
                scenario_key,
            )
            return await self._resolve_legacy(scenario_key, ctx, query, history, fallback_reason="no_db_version")

        # Phase3 预留：DecisionEngine 基于 tags 覆盖 version/params，此处先透传
        rule_trace: list[dict] = []
        if tags:
            rule_trace.append({"note": "decision_engine_not_implemented", "tags": list((tags or {}).keys())})

        # 加载所有引用的文档（一次性批量）
        docs_map: dict[str, tuple[str, Optional[int]]] = {}
        for spec in version.doc_refs or []:
            content, ver = await self.store.get_doc_text(spec.doc_key, spec.doc_version_id)
            docs_map[spec.doc_key] = (content, ver)

        system_text = render_system(
            template=version.template,
            ctx=ctx or {},
            docs_map=docs_map,
            doc_refs=version.doc_refs or [],
        )
        messages = build_messages(system_text, history, query)

        tools_enabled = self._decide_tools_enabled(version)
        meta = {
            "source": "db",
            "scenario_key": version.scenario_key,
            "scenario_name": version.scenario_name,
            "version_id": version.id,
            "version": version.version,
            "doc_versions": {
                spec.doc_key: docs_map.get(spec.doc_key, ("", None))[1]
                for spec in (version.doc_refs or [])
            },
            "tools_enabled": tools_enabled,
            "rule_trace": rule_trace,
            "system_len": len(system_text),
        }
        return PromptResolution(
            messages=messages,
            tools_enabled=tools_enabled,
            params=version.params or PromptParams(),
            meta=meta,
        )

    def _decide_tools_enabled(self, version: PromptVersionView) -> bool:
        """场景级 tools_enabled 与版本级 params.tools_enabled 的组合：
        - 场景级为 False 时强制关闭；
        - 否则版本级优先（None 时沿用场景级 True）。
        """
        if not version.scenario_tools_enabled:
            return False
        if version.params and version.params.tools_enabled is not None:
            return bool(version.params.tools_enabled)
        return True

    async def _resolve_legacy(
        self,
        scenario_key: str,
        ctx: dict,
        query: str,
        history: list[dict],
        fallback_reason: str = "flag_off",
    ) -> PromptResolution:
        # 惰性 import，避免循环依赖
        from ai.prompts import get_prompt_for_scenario
        system_text = get_prompt_for_scenario(scenario_key, ctx or {})
        messages = build_messages(system_text, history, query)
        return PromptResolution(
            messages=messages,
            tools_enabled=True,
            params=PromptParams(),
            meta={
                "source": "legacy",
                "scenario_key": scenario_key,
                "reason": fallback_reason,
                "system_len": len(system_text),
            },
        )
