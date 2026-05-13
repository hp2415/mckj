"""
PromptStore：提示词配置数据访问层（带进程内缓存 + TTL + 发布失效）。

对外仅暴露稳定的 async 接口：
- get_published_version(scenario_key) -> PromptVersionView | None
- get_doc_text(doc_key, version_id=None) -> (content: str, version: int | None)
- list_scenarios(enabled_only=True) -> list[dict]
- list_routable_scenarios(ui_category=None) -> list[RoutableScenarioView]
- invalidate() / invalidate_scenario(key) / invalidate_doc(key)

实现说明：
- 进程内单例：用 get_prompt_store() 获取全局实例，避免每次请求都新建。
- 失效策略：发布/回滚成功后由调用方主动 invalidate；TTL 兜底。
- 数据库访问使用短会话 (AsyncSessionLocal)，与请求链路的 get_db() 事务解耦，
  避免在长流式响应里读配置时与业务事务互相影响。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select, desc, exists

from core.logger import logger
from database import AsyncSessionLocal
from models import (
    PromptScenario,
    PromptVersion,
    PromptDoc,
    PromptDocVersion,
)

from ai.prompt_models import (
    PromptVersionView,
    template_from_json,
    doc_refs_from_json,
    params_from_json,
)
from ai.router_keyword_catalog import expand_keyword_refs, scenario_default_keyword_refs


@dataclass
class RoutableScenarioView:
    """场景路由器看到的"候选场景"视图（仅暴露路由需要的字段）。"""

    scenario_key: str
    name: str
    description: str
    ui_category: str
    tools_enabled: bool
    router_hints: dict = field(default_factory=dict)

    # ---- 便捷访问 router_hints 内的字段，避免下游每个调用方都做容错 ----
    @property
    def keywords(self) -> list[str]:
        v = self.router_hints.get("keywords") if isinstance(self.router_hints, dict) else None
        return [str(x).strip() for x in (v or []) if str(x).strip()]

    @property
    def anti_keywords(self) -> list[str]:
        v = self.router_hints.get("anti_keywords") if isinstance(self.router_hints, dict) else None
        return [str(x).strip() for x in (v or []) if str(x).strip()]

    @property
    def examples(self) -> list[str]:
        v = self.router_hints.get("examples") if isinstance(self.router_hints, dict) else None
        return [str(x).strip() for x in (v or []) if str(x).strip()]

    @property
    def anti_examples(self) -> list[str]:
        v = self.router_hints.get("anti_examples") if isinstance(self.router_hints, dict) else None
        return [str(x).strip() for x in (v or []) if str(x).strip()]

    @property
    def allowed_ui_categories(self) -> list[str]:
        v = self.router_hints.get("ui_categories") if isinstance(self.router_hints, dict) else None
        return [str(x).strip() for x in (v or []) if str(x).strip()]

    @property
    def requires_customer(self) -> Optional[bool]:
        if not isinstance(self.router_hints, dict):
            return None
        v = self.router_hints.get("requires_customer")
        if v is None:
            return None
        return bool(v)

    @property
    def priority(self) -> int:
        if not isinstance(self.router_hints, dict):
            return 0
        try:
            return int(self.router_hints.get("priority") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def keyword_refs(self) -> list[str]:
        v = self.router_hints.get("keyword_refs") if isinstance(self.router_hints, dict) else None
        return [str(x).strip() for x in (v or []) if str(x).strip()]

    @property
    def effective_keywords(self) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        refs = list(self.keyword_refs)
        refs.extend(scenario_default_keyword_refs(self.scenario_key))
        for kw in expand_keyword_refs(refs) + self.keywords:
            if kw and kw not in seen:
                seen.add(kw)
                merged.append(kw)
        return merged

    @property
    def customer_conditions(self) -> dict:
        v = self.router_hints.get("customer_conditions") if isinstance(self.router_hints, dict) else None
        return dict(v) if isinstance(v, dict) else {}

    @property
    def auxiliary_scenarios(self) -> list[str]:
        v = self.router_hints.get("auxiliary_scenarios") if isinstance(self.router_hints, dict) else None
        return [str(x).strip() for x in (v or []) if str(x).strip()]

    @property
    def compose_role(self) -> str:
        if not isinstance(self.router_hints, dict):
            return "primary"
        return str(self.router_hints.get("compose_role") or "primary").strip() or "primary"

    @property
    def compose_order(self) -> int:
        if not isinstance(self.router_hints, dict):
            return 0
        try:
            return int(self.router_hints.get("compose_order") or 0)
        except (TypeError, ValueError):
            return 0


# 进程内缓存 TTL（秒）：发布/回滚后会显式失效，此处只是兜底。
DEFAULT_TTL = 60.0


class PromptStore:
    def __init__(self, ttl: float = DEFAULT_TTL):
        self._ttl = ttl
        self._version_cache: dict[str, tuple[float, Optional[PromptVersionView]]] = {}
        # doc_key -> (exp_ts, content, version)
        self._doc_latest_cache: dict[str, tuple[float, str, Optional[int]]] = {}
        # (doc_key, version_id) -> (exp_ts, content, version)
        self._doc_fixed_cache: dict[tuple[str, int], tuple[float, str, Optional[int]]] = {}
        # ui_category("" 代表全集) -> (exp_ts, list[RoutableScenarioView])
        self._routable_cache: dict[str, tuple[float, list[RoutableScenarioView]]] = {}
        self._lock = asyncio.Lock()

    # ----------------- Scenario / Version -----------------

    async def get_published_version(self, scenario_key: str) -> Optional[PromptVersionView]:
        if not scenario_key:
            return None
        now = time.monotonic()
        cached = self._version_cache.get(scenario_key)
        if cached and cached[0] > now:
            return cached[1]

        async with self._lock:
            # 双检
            cached = self._version_cache.get(scenario_key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            view = await self._load_published_version(scenario_key)
            self._version_cache[scenario_key] = (time.monotonic() + self._ttl, view)
            return view

    async def _load_published_version(self, scenario_key: str) -> Optional[PromptVersionView]:
        async with AsyncSessionLocal() as db:
            stmt_s = select(PromptScenario).where(PromptScenario.scenario_key == scenario_key)
            res_s = await db.execute(stmt_s)
            scenario = res_s.scalars().first()
            if not scenario or not scenario.enabled:
                return None

            stmt_v = (
                select(PromptVersion)
                .where(PromptVersion.scenario_id == scenario.id)
                .where(PromptVersion.status == "published")
                .order_by(desc(PromptVersion.version))
                .limit(1)
            )
            res_v = await db.execute(stmt_v)
            ver = res_v.scalars().first()
            if not ver:
                return None
            return PromptVersionView(
                id=ver.id,
                scenario_key=scenario.scenario_key,
                scenario_name=scenario.name,
                scenario_tools_enabled=bool(scenario.tools_enabled),
                version=ver.version,
                status=ver.status,
                template=template_from_json(ver.template_json),
                doc_refs=doc_refs_from_json(ver.doc_refs_json),
                params=params_from_json(ver.params_json),
                notes=ver.notes,
            )

    async def list_scenarios(self, enabled_only: bool = True) -> list[dict]:
        async with AsyncSessionLocal() as db:
            stmt = select(PromptScenario).order_by(PromptScenario.id.asc())
            if enabled_only:
                stmt = stmt.where(PromptScenario.enabled == True)  # noqa: E712
            res = await db.execute(stmt)
            out = []
            for s in res.scalars().all():
                out.append({
                    "id": s.id,
                    "scenario_key": s.scenario_key,
                    "name": s.name,
                    "description": s.description,
                    "enabled": bool(s.enabled),
                    "tools_enabled": bool(s.tools_enabled),
                })
            return out

    # ----------------- 可路由场景目录（供 SceneRouter） -----------------

    async def list_routable_scenarios(
        self, ui_category: Optional[str] = None
    ) -> list[RoutableScenarioView]:
        """
        返回路由器可选的候选场景：
        - enabled=True
        - 至少存在一条 published 的 PromptVersion
        - 永远过滤 ui_category="backend_only"
        - 传入 ui_category 时只返回该入口下的场景；按场景自身 router_hints.ui_categories 再做一次约束
        - 按 router_hints.priority desc, id asc 排序，便于规则层稳定挑选
        """
        cache_key = (ui_category or "").strip().lower()
        now = time.monotonic()
        cached = self._routable_cache.get(cache_key)
        if cached and cached[0] > now:
            return list(cached[1])

        async with self._lock:
            cached = self._routable_cache.get(cache_key)
            if cached and cached[0] > time.monotonic():
                return list(cached[1])
            views = await self._load_routable_scenarios(cache_key)
            self._routable_cache[cache_key] = (time.monotonic() + self._ttl, views)
            return list(views)

    async def _load_routable_scenarios(self, ui_category: str) -> list[RoutableScenarioView]:
        async with AsyncSessionLocal() as db:
            has_published = exists(
                select(PromptVersion.id)
                .where(PromptVersion.scenario_id == PromptScenario.id)
                .where(PromptVersion.status == "published")
            )
            stmt = (
                select(PromptScenario)
                .where(PromptScenario.enabled == True)  # noqa: E712
                .where(PromptScenario.ui_category != "backend_only")
                .where(has_published)
                .order_by(PromptScenario.id.asc())
            )
            if ui_category in ("free_chat", "customer_chat"):
                stmt = stmt.where(PromptScenario.ui_category == ui_category)
            res = await db.execute(stmt)
            rows = res.scalars().all()
            views: list[RoutableScenarioView] = []
            for s in rows:
                hints = s.router_hints_json
                if not isinstance(hints, dict):
                    hints = {}
                v = RoutableScenarioView(
                    scenario_key=s.scenario_key,
                    name=s.name,
                    description=(s.description or ""),
                    ui_category=s.ui_category or "customer_chat",
                    tools_enabled=bool(s.tools_enabled),
                    router_hints=hints,
                )
                # 场景自身 router_hints.ui_categories 再做一次过滤：
                # 留空 = 不限制；非空 = 必须包含传入 ui_category。
                allowed = v.allowed_ui_categories
                if ui_category and allowed and ui_category not in allowed:
                    continue
                views.append(v)
            # 排序：priority 大优先，其次 id 顺序（list 已按 id asc，stable sort 即可）
            views.sort(key=lambda x: (-x.priority, x.scenario_key))
            return views

    # ----------------- Doc -----------------

    async def get_doc_text(self, doc_key: str, version_id: Optional[int] = None) -> tuple[str, Optional[int]]:
        """
        返回 (content, version)。
        version_id 非 None 时按指定版本取；None 时取当前 published 最高版本。
        读不到返回 ("", None)。
        """
        if not doc_key:
            return "", None
        now = time.monotonic()
        if version_id is None:
            cached = self._doc_latest_cache.get(doc_key)
            if cached and cached[0] > now:
                return cached[1], cached[2]
        else:
            ck = (doc_key, version_id)
            cached_f = self._doc_fixed_cache.get(ck)
            if cached_f and cached_f[0] > now:
                return cached_f[1], cached_f[2]

        async with self._lock:
            if version_id is None:
                cached = self._doc_latest_cache.get(doc_key)
                if cached and cached[0] > time.monotonic():
                    return cached[1], cached[2]
            else:
                ck = (doc_key, version_id)
                cached_f = self._doc_fixed_cache.get(ck)
                if cached_f and cached_f[0] > time.monotonic():
                    return cached_f[1], cached_f[2]

            content, version = await self._load_doc_text(doc_key, version_id)
            if version_id is None:
                self._doc_latest_cache[doc_key] = (time.monotonic() + self._ttl, content, version)
            else:
                self._doc_fixed_cache[(doc_key, version_id)] = (time.monotonic() + self._ttl, content, version)
            return content, version

    async def _load_doc_text(self, doc_key: str, version_id: Optional[int]) -> tuple[str, Optional[int]]:
        async with AsyncSessionLocal() as db:
            stmt_d = select(PromptDoc).where(PromptDoc.doc_key == doc_key)
            res_d = await db.execute(stmt_d)
            doc = res_d.scalars().first()
            if not doc:
                return "", None

            if version_id is not None:
                stmt_v = select(PromptDocVersion).where(PromptDocVersion.id == int(version_id))
            else:
                stmt_v = (
                    select(PromptDocVersion)
                    .where(PromptDocVersion.doc_id == doc.id)
                    .where(PromptDocVersion.status == "published")
                    .order_by(desc(PromptDocVersion.version))
                    .limit(1)
                )
            res_v = await db.execute(stmt_v)
            ver = res_v.scalars().first()
            if not ver:
                return "", None
            return (ver.content or ""), int(ver.version)

    # ----------------- 失效 -----------------

    async def invalidate(self) -> None:
        async with self._lock:
            self._version_cache.clear()
            self._doc_latest_cache.clear()
            self._doc_fixed_cache.clear()
            self._routable_cache.clear()
        # 场景目录失效时，连带把 SceneRouter 的决策短缓存也清掉，
        # 否则刚发布的 hints 在 30s 内不会立即反映到路由器决策。
        await _invalidate_router_decision_cache()
        logger.info("PromptStore: full cache invalidated")

    async def invalidate_scenario(self, scenario_key: str) -> None:
        if not scenario_key:
            return
        async with self._lock:
            self._version_cache.pop(scenario_key, None)
            # 任何场景的发布/启停/router_hints 变更都可能影响路由候选集，整张表失效
            self._routable_cache.clear()
        await _invalidate_router_decision_cache()

    async def invalidate_doc(self, doc_key: str) -> None:
        if not doc_key:
            return
        async with self._lock:
            self._doc_latest_cache.pop(doc_key, None)
            keys_to_pop = [k for k in self._doc_fixed_cache.keys() if k[0] == doc_key]
            for k in keys_to_pop:
                self._doc_fixed_cache.pop(k, None)


_store_singleton: Optional[PromptStore] = None


def get_prompt_store() -> PromptStore:
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = PromptStore()
    return _store_singleton


async def _invalidate_router_decision_cache() -> None:
    """安全地失效 SceneRouter 的决策短缓存（运营改 hints / 发布版本后立即生效）。

    采用惰性 import 避免 prompt_store ↔ scene_router 形成循环依赖；
    SceneRouter 还没初始化时就静默跳过。
    """
    try:
        from ai.scene_router import get_decision_cache  # 延迟 import
        await get_decision_cache().invalidate()
    except Exception:
        # 任何失败都不影响 PromptStore 自身的失效流程
        pass
