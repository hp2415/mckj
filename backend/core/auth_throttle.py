"""登录失败锁定与注册限流（进程内计数，单 worker 部署足够）。"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 15 * 60
REGISTER_MAX = 15
REGISTER_WINDOW_SECONDS = 15 * 60

_lock = threading.Lock()


@dataclass
class _LoginState:
    failures: int = 0
    locked_until: float = 0.0


_login_states: dict[str, _LoginState] = {}
_register_hits: dict[str, deque[float]] = defaultdict(deque)


def client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _now() -> float:
    return time.monotonic()


def _norm_username(username: str) -> str:
    return (username or "").strip().lower()


def _lock_detail(remaining_sec: int) -> str:
    minutes = max(1, (int(remaining_sec) + 59) // 60)
    return f"密码错误次数过多，请 {minutes} 分钟后再试"


def login_guard(username: str) -> None:
    key = _norm_username(username)
    if not key:
        return
    now = _now()
    with _lock:
        state = _login_states.get(key)
        if not state or state.locked_until <= now:
            return
        remaining = int(state.locked_until - now) + 1
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=_lock_detail(remaining),
        headers={"Retry-After": str(max(1, remaining))},
    )


def record_login_failure(username: str) -> None:
    """累计失败；达到上限或已锁定时抛出 429。"""
    key = _norm_username(username)
    if not key:
        return
    now = _now()
    with _lock:
        state = _login_states.get(key) or _LoginState()
        if state.locked_until > now:
            remaining = int(state.locked_until - now) + 1
        else:
            state.failures += 1
            remaining = 0
            if state.failures >= LOGIN_MAX_FAILURES:
                state.locked_until = now + LOGIN_LOCK_SECONDS
                state.failures = 0
                remaining = LOGIN_LOCK_SECONDS
            _login_states[key] = state
            if remaining <= 0:
                return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=_lock_detail(remaining),
        headers={"Retry-After": str(max(1, remaining))},
    )


def clear_login_failures(username: str) -> None:
    key = _norm_username(username)
    if not key:
        return
    with _lock:
        _login_states.pop(key, None)


def register_guard(request: Request) -> None:
    ip = client_ip(request)
    now = _now()
    with _lock:
        hits = _register_hits[ip]
        while hits and now - hits[0] > REGISTER_WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= REGISTER_MAX:
            retry = int(REGISTER_WINDOW_SECONDS - (now - hits[0])) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="注册过于频繁，请稍后再试",
                headers={"Retry-After": str(max(1, retry))},
            )
        hits.append(now)
