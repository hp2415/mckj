"""
SceneRouter：在 AIGateway 调 PromptService 之前，决定本轮对话用哪个 scenario_key。

设计原则（与 plan 一致）：
- 仅决策 scenario_key，不改 prompt 渲染、不改工具决策，全部沿用既有链路。
- 数据驱动：候选场景 + router_hints_json 都来自管理后台；
  新增场景只需在 sqladmin 填好 hints，路由器会立即把它纳入候选。
- 分级兜底：
    Stage 0 显式 hint 命中候选 → 直接采用
    Stage 1 规则评分（关键词 / 反例 / requires_customer / ui_categories + priority）
    Stage 2 小模型 LLM 分类（few-shot=router_hints.examples / anti_examples）
    Stage 3 全部失败 → fallback：优先 hint；否则 ui_category 下默认场景。
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
    parse_scenario_hint,
)
from ai.router_debug import log_route_candidates, log_route_decision


# 路由器小模型 system prompt：要求模型在候选场景中选一个，并以 JSON 输出。
ROUTER_SYSTEM_PROMPT = (
    "你是一个对话场景分发器，负责把销售员当前的一句话指派给最合适的『场景 key』。\n"
    "请严格按以下规则工作：\n"
    "1. 只能从我给出的『候选场景』列表里挑选 scenario_key，禁止杜撰、禁止返回列表外的值。\n"
    "2. 若多个候选都贴近，优先选用『描述/示例』更匹配的那个。\n"
    "3. 结合客户路由摘要判断新老客、意向档位等；必要时可同时返回 auxiliary_scenarios（辅场景 key 列表）。\n"
    "4. 任何时候只能输出一段 JSON，格式严格为："
    '{"scenario_key": "xxx", "auxiliary_scenarios": ["yyy"], "reason": "一句话解释"}。\n'
    "5. auxiliary_scenarios 可为空数组；辅场景也必须来自候选列表。\n"
    "6. 不要附加任何额外文字、Markdown 代码块、注释。"
)


# 单候选场景在 LLM prompt 中的格式
_CANDIDATE_TEMPLATE = (
    "- scenario_key: {key}\n"
    "  name: {name}\n"
    "  描述: {desc}\n"
    "  正例: {pos}\n"
    "  反例: {neg}"
)


# 规则匹配的简单字符判定（中文友好，所以不强制 \b 单词边界）
def _has_meaningful_customer_conditions(conditions: Optional[dict]) -> bool:
    if not conditions or not isinstance(conditions, dict):
        return False
    all_conds = conditions.get("all") if isinstance(conditions.get("all"), list) else []
    any_conds = conditions.get("any") if isinstance(conditions.get("any"), list) else []
    return bool(all_conds or any_conds)


def _has_substantive_rule_match(matched: list[dict], *, hint: str, scenario_key: str) -> bool:
    if hint and hint == scenario_key:
        return True
    return any(m.get("type") in ("keyword", "example") for m in matched)


def _contains(text: str, needle: str) -> bool:
    if not text or not needle:
        return False
    return needle.lower() in text.lower()


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
                )

            return await self._cache.get_or_compute(cache_key, _compute)

        return await self._classify_uncached(
            q_norm=q_norm,
            ui=ui,
            has_customer=has_customer,
            hint_norm=hint_norm,
            route_context=route_context,
            debug=debug,
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
    ) -> RouteDecision:
        ctx_dict = route_context.to_dict() if route_context else {}
        hint_keys, primary_hint = parse_scenario_hint(hint_norm)

        # 读取候选场景（DB + 缓存）
        try:
            candidates = await self.store.list_routable_scenarios(ui)
        except Exception as e:
            logger.warning("SceneRouter: 读取候选场景失败，走 fallback: {}", e)
            candidates = []

        cand_keys = [c.scenario_key for c in candidates]

        # ---------- Stage 0：显式 hint 命中候选 ----------
        if hint_keys:
            valid_hints = [k for k in hint_keys if k in cand_keys]
            if valid_hints:
                primary = valid_hints[0]
                aux = [k for k in valid_hints[1:] if k != primary]
                logger.debug(
                    "SceneRouter[hint]: 直接采用前端显式 scenario={} auxiliary={} ui_category={}",
                    primary,
                    aux,
                    ui,
                )
                return self._finalize_decision(
                    RouteDecision(
                        scenario_key=primary,
                        source="hint",
                        score=1.0,
                        reason=f"前端显式指定 scenario={primary}",
                        matched_rules=[{"type": "hint", "scenario_key": primary}],
                        candidates=cand_keys,
                        auxiliary_scenarios=aux,
                        route_context=ctx_dict,
                    ),
                    candidates,
                    query=q_norm,
                )

        if route_context and route_context.forbidden_outreach:
            filtered_out = [{
                "scenario_key": "*",
                "reason": "客户标记禁止打扰，主动话术场景被抑制",
            }]
            chosen = hint_norm if hint_norm and hint_norm != "auto" else self._default_for_ui(ui)
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
            # 一个候选都没有，直接兜底：尊重 hint > 默认
            chosen = primary_hint if primary_hint else self._default_for_ui(ui)
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
                    "keywords": c.effective_keywords,
                    "customer_conditions": c.customer_conditions,
                    "auxiliary_scenarios": c.auxiliary_scenarios,
                    "conditions_pass": evaluate_customer_conditions(route_context, c.customer_conditions),
                }
                for c in filtered
            ]
            log_route_candidates(candidates=eligible_rows, filtered_out=filtered_out)

        if not filtered:
            chosen = primary_hint if primary_hint else self._default_for_ui(ui)
            return RouteDecision(
                scenario_key=chosen if chosen in cand_keys else candidates[0].scenario_key,
                source="fallback",
                score=0.0,
                reason="所有候选都被 requires_customer / customer_conditions 过滤",
                candidates=cand_keys,
                filtered_out=filtered_out,
                route_context=ctx_dict,
            )

        # ---------- Stage 1：规则评分 ----------
        rule_decision, rule_disqualified = self._classify_by_rules(
            query=q_norm,
            candidates=filtered,
            hint=primary_hint,
            route_context=route_context,
        )
        if rule_decision is not None:
            rule_decision.candidates = cand_keys
            rule_decision.filtered_out = filtered_out + list(rule_decision.filtered_out)
            rule_decision.route_context = ctx_dict
            logger.debug(
                "SceneRouter[rule]: scenario={} score={:.2f} matched={}",
                rule_decision.scenario_key,
                rule_decision.score,
                rule_decision.matched_rules,
            )
            return self._finalize_decision(rule_decision, filtered, query=q_norm)

        filtered_out = filtered_out + list(rule_disqualified)

        # ---------- Stage 2：小模型分类 ----------
        if self.enabled and self.llm is not None and q_norm:
            try:
                llm_decision = await self._classify_by_llm(q_norm, filtered, route_context)
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

        # ---------- Stage 3：fallback ----------
        chosen = self._fallback_choice(primary_hint, filtered, ui)
        return self._finalize_decision(
            RouteDecision(
                scenario_key=chosen.scenario_key,
                source="fallback",
                score=0.0,
                reason="规则未命中且未启用/无可用小模型，回退到默认场景",
                matched_rules=[{"type": "fallback", "scenario_key": chosen.scenario_key}],
                candidates=cand_keys,
                filtered_out=filtered_out,
                route_context=ctx_dict,
            ),
            filtered,
            query=q_norm,
        )

    # --------- Stage 1：规则评分 ---------

    def _classify_by_rules(
        self,
        *,
        query: str,
        candidates: list[RoutableScenarioView],
        hint: str,
        route_context: Optional[RouteContext] = None,
    ) -> tuple[Optional[RouteDecision], list[dict]]:
        """简单加权评分：
        - 每个 keywords 命中 +1 分
        - examples 中能匹配到子串（≥4 字时）再 +1 分
        - anti_keywords / anti_examples 命中 → 直接置 -∞（淘汰）
        - 同分时 priority 高的优先，再不济按候选顺序
        - hint 等于某候选且未被淘汰 → 加 0.5 分（弱偏好）

        返回 (decision_or_none, disqualified_trace)。
        即便无人命中（decision=None），调用方仍可拿到淘汰 trace 合入 filtered_out。
        """
        if not query:
            return None, []

        scored: list[tuple[float, RoutableScenarioView, list[dict]]] = []
        # 规则层自己淘汰的候选（与前置 requires_customer 过滤分开记录，方便排障）
        disqualified_trace: list[dict] = []
        for c in candidates:
            matched: list[dict] = []
            score = 0.0
            disqualified = False
            disqualify_reason: Optional[dict] = None

            for kw in c.effective_keywords:
                if _contains(query, kw):
                    score += 1.0
                    matched.append({"type": "keyword", "value": kw, "scenario_key": c.scenario_key})

            for kw in c.anti_keywords:
                if _contains(query, kw):
                    disqualified = True
                    disqualify_reason = {
                        "scenario_key": c.scenario_key,
                        "reason": f"anti_keyword 命中：{kw}",
                    }
                    matched.append({"type": "anti_keyword", "value": kw, "scenario_key": c.scenario_key})
                    break

            if not disqualified:
                for ex in c.examples:
                    # 反向：示例片段子串命中 query；长示例只取关键短词，避免误判
                    if len(ex) >= 4 and _contains(query, ex):
                        score += 1.0
                        matched.append({"type": "example", "value": ex, "scenario_key": c.scenario_key})

                for ex in c.anti_examples:
                    if len(ex) >= 4 and _contains(query, ex):
                        disqualified = True
                        disqualify_reason = {
                            "scenario_key": c.scenario_key,
                            "reason": f"anti_example 命中：{ex[:24]}",
                        }
                        matched.append({"type": "anti_example", "value": ex, "scenario_key": c.scenario_key})
                        break

            if disqualified:
                if disqualify_reason:
                    disqualified_trace.append(disqualify_reason)
                continue

            if hint and hint == c.scenario_key:
                score += 0.5
                matched.append({"type": "hint_bias", "scenario_key": c.scenario_key})

            if route_context and _has_meaningful_customer_conditions(c.customer_conditions):
                if evaluate_customer_conditions(route_context, c.customer_conditions):
                    score += 0.25
                    matched.append({"type": "customer_condition", "scenario_key": c.scenario_key})

            scored.append((score, c, matched))

        if not scored:
            # 没人命中，但把淘汰 trace 一并返回，让 classify 决定怎么合入 filtered_out
            return None, disqualified_trace

        # 选最高分；同分按 priority desc，再按候选顺序（已稳定）
        scored.sort(key=lambda t: (-t[0], -t[1].priority))
        top_score, top_view, top_matched = scored[0]
        if top_score <= 0.0:
            return None, disqualified_trace
        if not _has_substantive_rule_match(top_matched, hint=hint, scenario_key=top_view.scenario_key):
            return None, disqualified_trace

        # 归一化分：基于该候选的关键词+示例总条数；保底 1 条
        max_possible = max(
            1.0,
            float(len(top_view.effective_keywords) + len(top_view.examples) + (0.5 if hint == top_view.scenario_key else 0.0)),
        )
        norm = min(1.0, top_score / max_possible)

        reason = f"规则命中 ({top_score:.2f} 分)"
        if hint == top_view.scenario_key:
            reason += "，含前端 hint 偏置"

        return RouteDecision(
            scenario_key=top_view.scenario_key,
            source="rule",
            score=norm,
            reason=reason,
            matched_rules=top_matched,
            filtered_out=disqualified_trace,
            auxiliary_scenarios=list(top_view.auxiliary_scenarios),
        ), disqualified_trace

    # --------- Stage 2：小模型 LLM 分类 ---------

    async def _classify_by_llm(
        self,
        query: str,
        candidates: list[RoutableScenarioView],
        route_context: Optional[RouteContext] = None,
    ) -> Optional[RouteDecision]:
        """让小模型在候选 scenario_key 中选一个。失败/越界返回 None。"""
        if not candidates:
            return None

        # 拼候选清单，长度上限 8 条，超过截断（极端业务也少有），保护 token 预算
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

        user_msg = (
            f"【候选场景】\n{cand_text}\n\n"
            f"【客户路由摘要】\n{(route_context.summary_text() if route_context else '（未绑定客户）')}\n\n"
            f"【用户当前发言】\n{query}\n\n"
            '请按规则只输出 JSON：{"scenario_key":"...","auxiliary_scenarios":[],"reason":"..."}'
        )
        messages = [
            {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

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
        q = (query or "").strip()
        if q:
            for c in candidates:
                if c.scenario_key == decision.scenario_key:
                    continue
                for kw in c.effective_keywords:
                    if _contains(q, kw):
                        if c.scenario_key not in aux:
                            aux.append(c.scenario_key)
                        break
        aux = list(dict.fromkeys(aux))
        decision.auxiliary_scenarios = aux
        decision.scenarios = [{"key": decision.scenario_key, "role": "primary"}]
        decision.scenarios.extend({"key": k, "role": "auxiliary"} for k in aux)
        return decision

    # --------- Stage 3：fallback ---------

    def _fallback_choice(
        self, hint: str, candidates: list[RoutableScenarioView], ui_category: str
    ) -> RoutableScenarioView:
        if hint and hint != "auto":
            for c in candidates:
                if c.scenario_key == hint:
                    return c
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
