"""
PromptStore：提示词配置数据访问层（带进程内缓存 + TTL + 发布失效）。

对外仅暴露稳定的 async 接口：
- get_published_version(scenario_key) -> PromptVersionView | None
- get_doc_text(doc_key, version_id=None) -> (content: str, version: int | None)
- list_scenarios(enabled_only=True) -> list[dict]
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
from typing import Optional

from sqlalchemy import select, desc

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
        logger.info("PromptStore: full cache invalidated")

    async def invalidate_scenario(self, scenario_key: str) -> None:
        if not scenario_key:
            return
        async with self._lock:
            self._version_cache.pop(scenario_key, None)

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
