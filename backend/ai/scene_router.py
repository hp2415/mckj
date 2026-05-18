"""
SceneRouter：在 AIGateway 调 PromptService 之前，决定本轮对话用哪个 scenario_key。

设计原则（与 plan 一致）：
- 仅决策 scenario_key，不改 prompt 渲染、不改工具决策，全部沿用既有链路。
- 数据驱动：候选场景 + router_hints_json 都来自管理后台；
  新增场景只需在 sqladmin 填好 hints，路由器会立即把它纳入候选。
- 分级兜底：
    Stage 0 小模型 LLM 分类（提示词由管理平台场景 ai_scene_router 发布；桌面 hint 仅作变量注入）
    Stage 1 全部失败 → fallback：ui_category 下默认场景。
- 失败安全：任何一层出错都不能中断主对话，必须返回一个可用的 scenario_key。

输出：RouteDecision，含命中来源 / 分数 / 命中规则 / 摘要 tags，
摘要 tags 会通过 PromptService.resolve(..., tags=tags) 透传到 prompt 元数据里，
供 SSE event=meta 回显与日志审计。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable, Optional

from core.logger import logger

from ai.llm_client import LLMClient
from ai.prompt_store import PromptStore, RoutableScenarioView, get_prompt_store
from ai.route_context import (
    RouteContext,
    evaluate_customer_conditions,
)
from ai.router_prompt import build_router_chat_messages
from ai.router_debug import log_route_candidates, log_route_decision


# 单候选场景在 LLM prompt 中的格式
_CANDIDATE_TEMPLATE = (
    "- scenario_key: {key}\n"
    "  name: {name}\n"
    "  描述: {desc}\n"
    "  正例: {pos}\n"
    "  反例: {neg}"
)


@dataclass
class RouteDecision:
    """场景路由器的最终决定。

    - scenario_key: 实际选用的场景 key（保证非空）
    - source:  hint / rule / llm / fallback —— 决策来自哪一层
    - score:   规则层归一化分（0-1）；LLM/fallback 给一个语义值
    - reason:  简短的中文说明，便于日志/排障/前端 toast
    - matched_rules: 命中的规则列表（可能多条），便于审计
    - candidates: 本轮参与决策的候选 scenario_key 列表
    - filtered_out: 被前置过滤/反向关键词淘汰的候选及原因（用于排障/调 hints）
    - model: 当 source=="llm" 时为路由器实际使用的小模型名；其它来源为 None
    - cached: 是否来自同一句话的复用决策（多模型并发时只算一次）
    """

    scenario_key: str
    source: str = "fallback"
    score: float = 0.0
    reason: str = ""
    matched_rules: list[dict] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    filtered_out: list[dict] = field(default_factory=list)
    model: Optional[str] = None
    cached: bool = False
    auxiliary_scenarios: list[str] = field(default_factory=list)
    scenarios: list[dict] = field(default_factory=list)
    route_context: dict = field(default_factory=dict)

    def to_meta_dict(self) -> dict:
        return {
            "source": self.source,
            "score": round(float(self.score), 3),
            "reason": self.reason,
            "rules": list(self.matched_rules),
            "candidates": list(self.candidates),
            "filtered_out": list(self.filtered_out),
            "model": self.model,
            "cached": self.cached,
            "auxiliary_scenarios": list(self.auxiliary_scenarios),
            "scenarios": list(self.scenarios),
            "route_context": dict(self.route_context),
        }

    def to_tags(self) -> dict:
        """透传给 PromptService.resolve(..., tags=...) 的简化标签。"""
        tags = {
            "router_source": self.source,
            "router_scenario": self.scenario_key,
            "router_score": round(float(self.score), 3),
        }
        if self.auxiliary_scenarios:
            tags["router_auxiliary"] = list(self.auxiliary_scenarios)
        return tags


# ============ 决策缓存：多模型并发去重 + 重发同一句话复用 ============
#
# 桌面端勾选 N 个对话模型时，会并发打 N 个 /api/ai/chat。
# 这 N 个请求的 (user_id, ui_category, has_customer, hint, query) 完全一致，
# 路由器没必要算 N 次。这里用一个进程内 TTL 字典 + asyncio Future 合并实现：
#
# - 同一时间最多有 1 个请求真正在跑路由器；其它请求共享它的结果。
# - 决策落 TTL 缓存后，后续 30s 内的同 key 请求直接取缓存（标记 cached=true）。
# - 任意一边失败时，第一名"所有者"把异常传给其它等待者；它们各自降级 fallback，
#   保证主对话永远不被路由器阻塞。
# - llm_router_model 切换 / hints 改动会通过 PromptStore.invalidate_scenario
#   清掉版本/路由目录缓存，但不直接清这一层；30s TTL 足以让"切完配置→对比效果"
#   这种节奏自动过期，不会造成长期错配。

DEFAULT_DECISION_CACHE_TTL = 30.0


class _DecisionCache:
    def __init__(self, ttl: float = DEFAULT_DECISION_CACHE_TTL):
        self.ttl = float(ttl)
        # key -> (expires_at, decision)
        self._done: dict[str, tuple[float, RouteDecision]] = {}
        # key -> in-flight Future（首个请求是 owner，其余 await 它）
        self._inflight: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def make_key(
        *,
        user_id: Optional[int],
        ui_category: str,
        has_customer: bool,
        hint: str,
        query: str,
        route_context_fp: str = "",
    ) -> str:
        # hint=="auto" 与空 hint 等价，统一归一化以便复用决策
        norm_hint = "" if (hint or "").strip().lower() in ("", "auto") else hint.strip()
        norm_query = (query or "").strip()
        h = hashlib.sha256()
        h.update(str(user_id or "").encode("utf-8"))
        h.update(b"|")
        h.update((ui_category or "").encode("utf-8"))
        h.update(b"|")
        h.update(b"1" if has_customer else b"0")
        h.update(b"|")
        h.update(norm_hint.encode("utf-8"))
        h.update(b"|")
        h.update(norm_query.encode("utf-8"))
        h.update(b"|")
        h.update((route_context_fp or "").encode("utf-8"))
        return h.hexdigest()

    async def get_or_compute(
        self,
        key: str,
        compute: Callable[[], Awaitable[RouteDecision]],
    ) -> RouteDecision:
        now = time.monotonic()
        # 1) 命中已完成缓存（快速路径，免锁）
        ent = self._done.get(key)
        if ent and ent[0] > now:
            return _mark_cached(ent[1])

        # 2) 加锁：双检 + 注册/挂载 inflight
        async with self._lock:
            ent = self._done.get(key)
            if ent and ent[0] > time.monotonic():
                return _mark_cached(ent[1])
            existing = self._inflight.get(key)
            if existing is not None and not existing.done():
                fut = existing
                am_owner = False
            else:
                fut = asyncio.get_event_loop().create_future()
                self._inflight[key] = fut
                am_owner = True

        # 3) 非 owner：直接等首个请求的结果
        if not am_owner:
            try:
                decision = await fut
                return _mark_cached(decision)
            except Exception:
                # owner 挂了，我自己降级重算一次（避免与 owner 抢锁，直接走计算路径）
                logger.warning("SceneRouter: 等待 inflight 决策时上游失败，本请求独立重算")
                try:
                    return await compute()
                except Exception:
                    raise

        # 4) owner：跑 compute，把结果写缓存并通知等待者
        try:
            decision = await compute()
        except Exception as e:
            async with self._lock:
                f = self._inflight.pop(key, None)
            if f is not None and not f.done():
                f.set_exception(e)
            raise
        async with self._lock:
            self._done[key] = (time.monotonic() + self.ttl, decision)
            f = self._inflight.pop(key, None)
        if f is not None and not f.done():
            f.set_result(decision)
        return decision

    async def invalidate(self) -> None:
        async with self._lock:
            self._done.clear()


def _mark_cached(d: RouteDecision) -> RouteDecision:
    """返回 d 的浅拷贝并把 cached 置为 True。不变更原决策。"""
    return replace(
        d,
        cached=True,
        matched_rules=list(d.matched_rules),
        candidates=list(d.candidates),
        filtered_out=list(d.filtered_out),
        auxiliary_scenarios=list(d.auxiliary_scenarios),
        scenarios=list(d.scenarios),
        route_context=dict(d.route_context),
    )


_decision_cache_singleton: Optional[_DecisionCache] = None


def get_decision_cache() -> _DecisionCache:
    """模块级单例。重启进程即失效；不打算持久化。"""
    global _decision_cache_singleton
    if _decision_cache_singleton is None:
        _decision_cache_singleton = _DecisionCache()
    return _decision_cache_singleton


class SceneRouter:
    """场景路由器。同实例可复用（无状态，仅持有 store / llm_router 引用）。"""

    def __init__(
        self,
        store: Optional[PromptStore] = None,
        llm_router: Optional[LLMClient] = None,
        *,
        enabled: bool = True,
        use_cache: bool = True,
    ):
        self.store = store or get_prompt_store()
        self.llm = llm_router
        self.enabled = bool(enabled)
        # 多模型并发去重缓存；测试时可关闭
        self._cache = get_decision_cache() if use_cache else None

    async def classify(
        self,
        *,
        query: str,
        ui_category: str,
        has_customer: bool,
        hint: Optional[str] = None,
        customer_tags: Optional[dict] = None,
        user_id: Optional[int] = None,
        route_context: Optional[RouteContext] = None,
        debug: bool = False,
        db=None,
    ) -> RouteDecision:
        """决定 scenario_key。任何异常都会被吞掉并走 fallback。

        多模型并发：当同一 user 在 30s 内对同一句话发起多次请求（典型场景：
        桌面端勾了 N 个对话模型并发对比效果），路由器只会真正计算一次，
        其它请求共享结果并把返回值的 cached 标记为 True。
        """
        q_norm = (query or "").strip()
        ui = (ui_category or "").strip().lower() or "customer_chat"
        hint_norm = (hint or "").strip()
        ctx_fp = route_context.fingerprint() if route_context else ""

        if self._cache is not None:
            cache_key = _DecisionCache.make_key(
                user_id=user_id,
                ui_category=ui,
                has_customer=has_customer,
                hint=hint_norm,
                query=q_norm,
                route_context_fp=ctx_fp,
            )

            async def _compute() -> RouteDecision:
                return await self._classify_uncached(
                    q_norm=q_norm,
                    ui=ui,
                    has_customer=has_customer,
                    hint_norm=hint_norm,
                    route_context=route_context,
                    debug=debug,
                    db=db,
                )

            return await self._cache.get_or_compute(cache_key, _compute)

        return await self._classify_uncached(
            q_norm=q_norm,
            ui=ui,
            has_customer=has_customer,
            hint_norm=hint_norm,
            route_context=route_context,
            debug=debug,
            db=db,
        )

    async def _classify_uncached(
        self,
        *,
        q_norm: str,
        ui: str,
        has_customer: bool,
        hint_norm: str,
        route_context: Optional[RouteContext] = None,
        debug: bool = False,
        db=None,
    ) -> RouteDecision:
        ctx_dict = route_context.to_dict() if route_context else {}

        # 读取候选场景（DB + 缓存）
        try:
            candidates = await self.store.list_routable_scenarios(ui)
        except Exception as e:
            logger.warning("SceneRouter: 读取候选场景失败，走 fallback: {}", e)
            candidates = []

        cand_keys = [c.scenario_key for c in candidates]

        if route_context and route_context.forbidden_outreach:
            filtered_out = [{
                "scenario_key": "*",
                "reason": "客户标记禁止打扰，主动话术场景被抑制",
            }]
            chosen = self._default_for_ui(ui)
            return RouteDecision(
                scenario_key=chosen if chosen in cand_keys else self._default_for_ui(ui),
                source="fallback",
                score=0.0,
                reason="客户标记禁止打扰，回退到默认场景",
                candidates=cand_keys,
                filtered_out=filtered_out,
                route_context=ctx_dict,
            )

        if not candidates:
            chosen = self._default_for_ui(ui)
            return RouteDecision(
                scenario_key=chosen,
                source="fallback",
                score=0.0,
                reason="无可用候选（候选目录为空或 DB 未配置场景）",
                candidates=cand_keys,
                route_context=ctx_dict,
            )

        # ---------- 前置过滤：requires_customer ----------
        filtered: list[RoutableScenarioView] = []
        filtered_out: list[dict] = []
        for c in candidates:
            req = c.requires_customer
            if req is True and not has_customer:
                filtered_out.append({
                    "scenario_key": c.scenario_key,
                    "reason": "requires_customer=true 但未选客户",
                })
                continue
            if req is False and has_customer:
                filtered_out.append({
                    "scenario_key": c.scenario_key,
                    "reason": "requires_customer=false 但已选客户",
                })
                continue
            if not evaluate_customer_conditions(route_context, c.customer_conditions):
                filtered_out.append({
                    "scenario_key": c.scenario_key,
                    "reason": "customer_conditions 未满足",
                })
                continue
            filtered.append(c)

        if debug:
            eligible_rows = [
                {
                    "scenario_key": c.scenario_key,
                    "priority": c.priority,
                    "customer_conditions": c.customer_conditions,
                    "auxiliary_scenarios": c.auxiliary_scenarios,
                    "conditions_pass": evaluate_customer_conditions(route_context, c.customer_conditions),
                }
                for c in filtered
            ]
            log_route_candidates(candidates=eligible_rows, filtered_out=filtered_out)

        if not filtered:
            chosen = self._default_for_ui(ui)
            return RouteDecision(
                scenario_key=chosen if chosen in cand_keys else candidates[0].scenario_key,
                source="fallback",
                score=0.0,
                reason="所有候选都被 requires_customer / customer_conditions 过滤",
                candidates=cand_keys,
                filtered_out=filtered_out,
                route_context=ctx_dict,
            )

        # ---------- Stage 1：小模型分类 ----------
        if self.enabled and self.llm is not None and q_norm:
            try:
                llm_decision = await self._classify_by_llm(
                    q_norm,
                    filtered,
                    route_context,
                    ui_category=ui,
                    scenario_hint=hint_norm,
                    db=db,
                )
                if llm_decision is not None:
                    llm_decision.candidates = cand_keys
                    llm_decision.filtered_out = list(filtered_out)
                    llm_decision.route_context = ctx_dict
                    logger.debug(
                        "SceneRouter[llm]: scenario={} score={:.2f} reason={} model={}",
                        llm_decision.scenario_key,
                        llm_decision.score,
                        llm_decision.reason,
                        llm_decision.model,
                    )
                    return self._finalize_decision(llm_decision, filtered, query=q_norm)
            except Exception as e:
                logger.warning("SceneRouter: 小模型分类失败，走 fallback: {}", e)

        # ---------- Stage 2：fallback ----------
        chosen = self._fallback_choice(filtered, ui)
        return self._finalize_decision(
            RouteDecision(
                scenario_key=chosen.scenario_key,
                source="fallback",
                score=0.0,
                reason="小模型未启用/无可用客户端/分类失败，回退到默认场景",
                matched_rules=[{"type": "fallback", "scenario_key": chosen.scenario_key}],
                candidates=cand_keys,
                filtered_out=filtered_out,
                route_context=ctx_dict,
            ),
            filtered,
            query=q_norm,
        )

    async def _classify_by_llm(
        self,
        query: str,
        candidates: list[RoutableScenarioView],
        route_context: Optional[RouteContext] = None,
        *,
        ui_category: str = "customer_chat",
        scenario_hint: str = "",
        db=None,
    ) -> Optional[RouteDecision]:
        """让小模型在候选 scenario_key 中选一个。失败/越界返回 None。"""
        if not candidates:
            return None

        view_subset = candidates[:8]
        cand_text_blocks = []
        for c in view_subset:
            pos = " / ".join(c.examples[:3]) if c.examples else "（未提供）"
            neg = " / ".join(c.anti_examples[:3]) if c.anti_examples else "（未提供）"
            cand_text_blocks.append(
                _CANDIDATE_TEMPLATE.format(
                    key=c.scenario_key,
                    name=c.name,
                    desc=(c.description or "")[:120],
                    pos=pos,
                    neg=neg,
                )
            )
        cand_text = "\n".join(cand_text_blocks)
        valid_keys = {c.scenario_key for c in view_subset}
        summary = route_context.summary_text() if route_context else "（未绑定客户）"

        if db is None:
            from database import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                messages, _ = await build_router_chat_messages(
                    session,
                    candidates_block=cand_text,
                    route_context_summary=summary,
                    user_query=query,
                    ui_category=ui_category,
                    scenario_hint=scenario_hint,
                )
        else:
            messages, _ = await build_router_chat_messages(
                db,
                candidates_block=cand_text,
                route_context_summary=summary,
                user_query=query,
                ui_category=ui_category,
                scenario_hint=scenario_hint,
            )

        try:
            resp = await self.llm.chat(messages=messages, temperature=0.0, max_tokens=128)
        except Exception as e:
            logger.warning("SceneRouter LLM.chat 调用失败: {}", e)
            return None

        try:
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        except Exception:
            content = ""
        chosen_key, reason, aux_keys = _parse_llm_choice(content, valid_keys)
        if not chosen_key:
            return None

        router_model = getattr(self.llm, "model", None) if self.llm else None
        return RouteDecision(
            scenario_key=chosen_key,
            source="llm",
            score=0.85,
            reason=reason or "由小模型分类器选定",
            matched_rules=[{"type": "llm", "scenario_key": chosen_key}],
            model=router_model,
            auxiliary_scenarios=aux_keys,
        )

    def _finalize_decision(
        self,
        decision: RouteDecision,
        candidates: list[RoutableScenarioView],
        query: str = "",
    ) -> RouteDecision:
        aux = [k for k in (decision.auxiliary_scenarios or []) if k and k != decision.scenario_key]
        if not aux:
            for c in candidates:
                if c.scenario_key == decision.scenario_key:
                    aux = [k for k in c.auxiliary_scenarios if k and k != decision.scenario_key]
                    break
        aux = list(dict.fromkeys(aux))
        decision.auxiliary_scenarios = aux
        decision.scenarios = [{"key": decision.scenario_key, "role": "primary"}]
        decision.scenarios.extend({"key": k, "role": "auxiliary"} for k in aux)
        return decision

    # --------- fallback ---------

    def _fallback_choice(
        self, candidates: list[RoutableScenarioView], ui_category: str
    ) -> RoutableScenarioView:
        default_key = self._default_for_ui(ui_category)
        for c in candidates:
            if c.scenario_key == default_key:
                return c
        return candidates[0]

    @staticmethod
    def _default_for_ui(ui_category: str) -> str:
        if ui_category == "free_chat":
            return "staff_assistant"
        return "general_chat"


# ============ JSON 解析工具：从 LLM 输出里"鲁棒地"提取 scenario_key ============

_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*?\}")


def _parse_llm_choice(content: str, valid_keys: set[str]) -> tuple[Optional[str], str, list[str]]:
    """优先严格 JSON 解析；失败时退化为正则抽取首个 JSON 对象；最后纯文本兜底。"""
    if not content:
        return None, "", []
    text = content.strip()

    def _read_obj(obj: dict) -> tuple[Optional[str], str, list[str]]:
        k = (obj.get("scenario_key") or "").strip()
        r = (obj.get("reason") or "").strip()
        aux_raw = obj.get("auxiliary_scenarios") or []
        aux: list[str] = []
        if isinstance(aux_raw, list):
            for item in aux_raw:
                key = str(item).strip()
                if key in valid_keys and key not in aux:
                    aux.append(key)
        if k in valid_keys:
            return k, r, aux
        return None, r, aux

    # 1) 直接 JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            parsed = _read_obj(obj)
            if parsed[0]:
                return parsed
    except Exception:
        pass

    # 2) 文本中找一个 JSON
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                parsed = _read_obj(obj)
                if parsed[0]:
                    return parsed
        except Exception:
            pass

    # 3) 纯文本：模型直接吐出 scenario_key 字符串
    for k in valid_keys:
        if k and k in text:
            return k, "", []
    return None, "", []
