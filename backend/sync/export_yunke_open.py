"""
云客开放平台全量导出（不连接、不写入当前数据库）。

从另一套开放平台凭证拉取：
  - companyAccounts      → accounts.jsonl
  - getAllFriendsIncrement → friends.jsonl（按自然日）
  - allRecords           → chats.jsonl
  - queryWeChatVoiceListByCursor → voice.jsonl

输出目录另含 manifest.json / state.json，同 --out 可断点续跑。
四类实体默认并行导出，共享 ≥5.1s 请求限频（避免开放平台「请勿频繁操作」）。

用法：
  cd backend
  python -m sync.export_yunke_open \\
    --out D:/exports/yunke_other_acct \\
    --base-url https://open.xxx.com \\
    --company XXX \\
    --key XXX \\
    --partner-id XXX \\
    --friends-from 2024-01-01 \\
    --friends-to 2026-07-28 \\
    --entities accounts,friends,chats,voice

凭证也可设环境变量（优先 YUNKE_EXPORT_*，回退 WECHAT_OPEN_*）：
  YUNKE_EXPORT_BASE_URL / COMPANY / KEY / PARTNER_ID
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

TZ_SH = ZoneInfo("Asia/Shanghai")
RATE_LIMIT_SEC = 5.1
RATE_LIMIT_RETRIES = 5
NETWORK_RETRY_BASE_SEC = 5.0
NETWORK_RETRY_MAX_SEC = 60.0
ALL_ENTITIES = ("accounts", "friends", "chats", "voice")


class _RateGate:
    """Ensure ≥ interval seconds between acquires across concurrent exporters."""

    def __init__(self, interval: float = RATE_LIMIT_SEC) -> None:
        self.interval = float(interval)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self.interval - (now - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


def _is_rate_limit_message(msg: Any) -> bool:
    s = str(msg or "")
    return ("频繁" in s) or ("5秒" in s) or ("5 秒" in s)


def _is_retryable_http_error(exc: Exception) -> bool:
    """仅重试临时网络错误；4xx 参数/鉴权错误仍立即失败。"""
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException, OSError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 429, 500, 502, 503, 504)
    return False


@dataclass
class ExportCtx:
    """Shared context for parallel exporters (rate limit + state file lock)."""

    out_dir: Path
    state: ExportState
    state_path: Path
    rate_gate: _RateGate
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def save_state(self) -> None:
        async with self.state_lock:
            _save_state(self.state_path, self.state)

    async def note_error(self, err: str) -> None:
        async with self.state_lock:
            self.state.errors.append(err)
            _save_state(self.state_path, self.state)


# ---------------------------------------------------------------------------
# credentials / signing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenCreds:
    base_url: str
    company: str
    key: str
    partner_id: str


def _md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def _env(*names: str) -> str:
    for n in names:
        v = (os.getenv(n) or "").strip()
        if v:
            return v
    return ""


def resolve_creds(
    *,
    base_url: str | None,
    company: str | None,
    key: str | None,
    partner_id: str | None,
) -> OpenCreds:
    base = (base_url or "").strip().rstrip("/") or _env(
        "YUNKE_EXPORT_BASE_URL", "WECHAT_OPEN_BASE_URL"
    )
    company_v = (company or "").strip() or _env(
        "YUNKE_EXPORT_COMPANY", "WECHAT_OPEN_COMPANY"
    )
    key_v = (key or "").strip() or _env("YUNKE_EXPORT_KEY", "WECHAT_OPEN_KEY")
    partner = (partner_id or "").strip() or _env(
        "YUNKE_EXPORT_PARTNER_ID", "WECHAT_OPEN_ADMIN_PARTNER_ID"
    )
    missing = [
        n
        for n, v in (
            ("base_url", base),
            ("company", company_v),
            ("key", key_v),
            ("partner_id", partner),
        )
        if not v
    ]
    if missing:
        raise SystemExit(
            "缺少凭证："
            + ", ".join(missing)
            + "（CLI 或 YUNKE_EXPORT_* / WECHAT_OPEN_* 环境变量）"
        )
    return OpenCreds(base_url=base, company=company_v, key=key_v, partner_id=partner)


def _auth_headers(creds: OpenCreds) -> dict[str, str]:
    ts_ms = str(int(time.time() * 1000))
    sign = _md5_upper(creds.key + creds.company + creds.partner_id + ts_ms)
    return {
        "company": creds.company,
        "partnerId": creds.partner_id,
        "timestamp": ts_ms,
        "key": creds.key,
        "sign": sign,
        "content-type": "application/json",
    }


def _mask(s: str, keep: int = 2) -> str:
    if not s:
        return ""
    if len(s) <= keep * 2:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep * 2) + s[-keep:]


# ---------------------------------------------------------------------------
# JSONL / state / manifest
# ---------------------------------------------------------------------------


@dataclass
class ExportState:
    accounts_done: bool = False
    accounts_next_page: int = 1
    accounts_rows: int = 0
    friends_done: bool = False
    friends_next_day: str = ""  # YYYY-MM-DD to start/continue
    friends_rows: int = 0
    chats_done: bool = False
    chat_cursor_time_ms: int = 0
    chat_cursor_create_ts_ms: int = 0
    chats_rows: int = 0
    voice_done: bool = False
    voice_next_id: int | None = None
    voice_started: bool = False  # False = first page with null nextId
    voice_rows: int = 0
    errors: list[str] = field(default_factory=list)


def _load_state(path: Path) -> ExportState:
    if not path.is_file():
        return ExportState()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ExportState(
        accounts_done=bool(raw.get("accounts_done")),
        accounts_next_page=int(raw.get("accounts_next_page") or 1),
        accounts_rows=int(raw.get("accounts_rows") or 0),
        friends_done=bool(raw.get("friends_done")),
        friends_next_day=str(raw.get("friends_next_day") or ""),
        friends_rows=int(raw.get("friends_rows") or 0),
        chats_done=bool(raw.get("chats_done")),
        chat_cursor_time_ms=int(raw.get("chat_cursor_time_ms") or 0),
        chat_cursor_create_ts_ms=int(raw.get("chat_cursor_create_ts_ms") or 0),
        chats_rows=int(raw.get("chats_rows") or 0),
        voice_done=bool(raw.get("voice_done")),
        voice_next_id=(
            int(raw["voice_next_id"])
            if raw.get("voice_next_id") is not None and str(raw.get("voice_next_id")).strip() != ""
            else None
        ),
        voice_started=bool(raw.get("voice_started")),
        voice_rows=int(raw.get("voice_rows") or 0),
        errors=list(raw.get("errors") or []),
    )


def _save_state(path: Path, state: ExportState) -> None:
    path.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return len(rows)


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# time helpers (friends)
# ---------------------------------------------------------------------------


def _parse_dt_loose(v: Any) -> datetime | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{10,}", s):
        ms = int(s)
        if ms >= 10**15:
            ms //= 1000
        if ms >= 10**12:
            ms //= 1000
        try:
            return datetime.fromtimestamp(ms, tz=TZ_SH).replace(tzinfo=None)
        except (OSError, ValueError):
            return None
    if len(s) >= 19:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return None


def _day_bounds_naive(calendar_day: str) -> tuple[datetime, datetime]:
    d = datetime.strptime(calendar_day.strip(), "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, 0, 0, 0)
    end = datetime(d.year, d.month, d.day, 23, 59, 59)
    return start, end


def _now_sh_naive() -> datetime:
    return datetime.now(tz=TZ_SH).replace(tzinfo=None)


def _api_start_time_cap(now_naive: datetime) -> datetime:
    return now_naive - timedelta(seconds=6)


def _item_reference_time(item: dict[str, Any], query_mode: str) -> datetime | None:
    if query_mode == "updateTime":
        return _parse_dt_loose(item.get("updateTime")) or _parse_dt_loose(item.get("createTime"))
    return _parse_dt_loose(item.get("createTime")) or _parse_dt_loose(item.get("updateTime"))


def _next_start_time(query_end_time: str | None) -> str | None:
    dt = _parse_dt_loose(query_end_time)
    if not dt:
        return None
    return (dt + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")


def _iter_days(from_day: date, to_day: date):
    cur = from_day
    while cur <= to_day:
        yield cur.isoformat()
        cur += timedelta(days=1)


def _yesterday_sh() -> date:
    return (_now_sh_naive().date() - timedelta(days=1))


def _default_friends_from() -> date:
    return _yesterday_sh() - timedelta(days=365)


# ---------------------------------------------------------------------------
# HTTP posts
# ---------------------------------------------------------------------------


async def post_company_accounts(
    client: httpx.AsyncClient,
    creds: OpenCreds,
    *,
    page_index: int,
    page_size: int,
) -> dict[str, Any]:
    url = f"{creds.base_url}/open/wechat/companyAccounts"
    payload = {
        "pageIndex": max(1, int(page_index)),
        "pageSize": min(400, max(1, int(page_size))),
    }
    resp = await client.post(url, json=payload, headers=_auth_headers(creds), timeout=60.0)
    resp.raise_for_status()
    return resp.json()


async def post_friends_increment(
    client: httpx.AsyncClient,
    creds: OpenCreds,
    payload: dict[str, Any],
) -> dict[str, Any]:
    url = f"{creds.base_url}/open/wechat/getAllFriendsIncrement"
    resp = await client.post(url, json=payload, headers=_auth_headers(creds), timeout=60.0)
    resp.raise_for_status()
    return resp.json()


async def post_all_records(
    client: httpx.AsyncClient,
    creds: OpenCreds,
    *,
    timestamp_ms: int,
    create_timestamp_ms: int,
) -> dict[str, Any]:
    url = f"{creds.base_url}/open/wechat/allRecords"
    payload = {
        "timestamp": int(timestamp_ms),
        "createTimestamp": int(create_timestamp_ms or 0),
    }
    resp = await client.post(url, json=payload, headers=_auth_headers(creds), timeout=60.0)
    resp.raise_for_status()
    return resp.json()


async def post_voice_page(
    client: httpx.AsyncClient,
    creds: OpenCreds,
    *,
    page_size: int,
    next_id: int | None,
    call_type: int | None,
    is_room: int | None,
) -> dict[str, Any]:
    url = f"{creds.base_url}/open/wechat/queryWeChatVoiceListByCursor"
    # voice API headers: sign without key field in some sync code — keep key for consistency with others
    headers = _auth_headers(creds)
    payload: dict[str, Any] = {"pageSize": int(page_size)}
    if next_id is not None:
        payload["nextId"] = int(next_id)
    if call_type is not None:
        payload["callType"] = int(call_type)
    if is_room is not None:
        payload["isRoom"] = int(is_room)
    resp = await client.post(url, json=payload, headers=headers, timeout=90.0)
    resp.raise_for_status()
    return resp.json()


def _strip_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def rows_from_company_accounts_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not body.get("success"):
        return out
    data = body.get("data") or {}
    page_groups = data.get("page")
    if not isinstance(page_groups, list):
        return out
    for group in page_groups:
        if not isinstance(group, dict):
            continue
        user_phone = _strip_or_none(group.get("userPhone"))
        user_id = _strip_or_none(group.get("userId"))
        account_code = user_id or user_phone
        items = group.get("data")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            wid = _strip_or_none(item.get("wechatId"))
            if not wid:
                continue
            out.append(
                {
                    "sales_wechat_id": wid,
                    "nickname": _strip_or_none(item.get("nickname")),
                    "alias_name": _strip_or_none(item.get("alias")),
                    "account_code": account_code,
                    "phone": _strip_or_none(item.get("phone")),
                    "user_id": user_id,
                    "user_phone": user_phone,
                }
            )
    return out


def _max_queryable_time_ms() -> int:
    return int(time.time() * 1000) - 40 * 60 * 1000


def _log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# exporters
# ---------------------------------------------------------------------------


async def _open_json_with_retry(
    rate_gate: _RateGate,
    *,
    label: str,
    call,
) -> dict[str, Any]:
    """调用开放平台；限频响应有限重试，临时网络故障持续重试。"""
    last_err = ""
    rate_attempt = 0
    network_failures = 0
    while True:
        await rate_gate.wait()
        try:
            body = await call()
            network_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not _is_retryable_http_error(exc):
                raise
            network_failures += 1
            delay = min(
                NETWORK_RETRY_MAX_SEC,
                NETWORK_RETRY_BASE_SEC * (2 ** min(network_failures - 1, 4)),
            )
            _log(
                f"[{label}] transient network error: {exc}; "
                f"retry #{network_failures} after {delay:.0f}s"
            )
            await asyncio.sleep(delay)
            continue

        if body.get("success"):
            return body
        msg = body.get("message")
        last_err = str(msg or "unknown error")
        rate_attempt += 1
        if _is_rate_limit_message(msg) and rate_attempt < RATE_LIMIT_RETRIES:
            _log(
                f"[{label}] rate-limited, retry {rate_attempt}/{RATE_LIMIT_RETRIES} "
                f"after {RATE_LIMIT_SEC}s"
            )
            await asyncio.sleep(RATE_LIMIT_SEC)
            continue
        raise RuntimeError(f"{label}: {last_err}")


async def export_accounts(
    client: httpx.AsyncClient,
    creds: OpenCreds,
    ctx: ExportCtx,
    *,
    page_size: int = 200,
) -> None:
    state = ctx.state
    if state.accounts_done:
        _log(f"[accounts] skip (done, rows={state.accounts_rows})")
        return

    path = ctx.out_dir / "accounts.jsonl"
    page_index = max(1, state.accounts_next_page)
    total_pages = page_index  # updated after first response

    while page_index <= total_pages:
        body = await _open_json_with_retry(
            ctx.rate_gate,
            label=f"accounts page={page_index}",
            call=lambda pi=page_index: post_company_accounts(
                client, creds, page_index=pi, page_size=page_size
            ),
        )

        data = body.get("data") or {}
        if isinstance(data, dict):
            total_pages = max(1, int(data.get("pageCount") or 1))

        rows = rows_from_company_accounts_body(body)
        n = _append_jsonl(path, rows)
        async with ctx.state_lock:
            state.accounts_rows += n
            state.accounts_next_page = page_index + 1
            _save_state(ctx.state_path, state)
        _log(
            f"[accounts] page {page_index}/{total_pages} +{n} total={state.accounts_rows}"
        )
        page_index += 1

    async with ctx.state_lock:
        state.accounts_done = True
        _save_state(ctx.state_path, state)
    _log(f"[accounts] done rows={state.accounts_rows}")


async def _export_friends_one_day(
    client: httpx.AsyncClient,
    creds: OpenCreds,
    path: Path,
    rate_gate: _RateGate,
    *,
    calendar_day: str,
    friend_types: tuple[int, ...],
    query_mode: str,
) -> int:
    day_start, day_end = _day_bounds_naive(calendar_day)
    now_naive = _now_sh_naive()
    filter_end = min(day_end, _api_start_time_cap(now_naive))
    if filter_end < day_start:
        _log(f"[friends] day={calendar_day} skip (beyond queryable window)")
        return 0

    rows_written = 0
    for friend_type in friend_types:
        cursor = day_start
        cap = _api_start_time_cap(_now_sh_naive())
        if cursor > cap:
            _log(f"[friends] day={calendar_day} type={friend_type} skip (start > cap)")
            continue

        while True:
            start_str = cursor.strftime("%Y-%m-%d %H:%M:%S")
            payload = {
                "type": friend_type,
                "getFirstData": False,
                "queryMode": query_mode,
                "startTime": start_str,
            }
            label = f"friends day={calendar_day} type={friend_type} start={start_str}"
            body = await _open_json_with_retry(
                rate_gate,
                label=label,
                call=lambda p=payload: post_friends_increment(client, creds, p),
            )

            data = body.get("data") or {}
            items = data.get("data") or []
            if not isinstance(items, list):
                items = []

            batch: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                ref = _item_reference_time(item, query_mode)
                if ref is not None and (ref < day_start or ref > filter_end):
                    continue
                row = dict(item)
                row["_export_calendar_day"] = calendar_day
                row["_export_friend_type"] = friend_type
                batch.append(row)

            rows_written += _append_jsonl(path, batch)

            q_end = data.get("queryEndTime")
            nxt = _next_start_time(q_end)
            if not nxt:
                break
            nxt_dt = _parse_dt_loose(nxt)
            if not nxt_dt or nxt_dt > day_end:
                break
            cursor = nxt_dt
            if cursor > _api_start_time_cap(_now_sh_naive()):
                break
            if not items:
                break

    return rows_written


async def export_friends(
    client: httpx.AsyncClient,
    creds: OpenCreds,
    ctx: ExportCtx,
    *,
    friends_from: date,
    friends_to: date,
    include_groups: bool,
    query_mode: str,
) -> None:
    state = ctx.state
    if state.friends_done:
        _log(f"[friends] skip (done, rows={state.friends_rows})")
        return

    path = ctx.out_dir / "friends.jsonl"
    types: tuple[int, ...] = (1, 2) if include_groups else (1,)

    start_day = friends_from
    if state.friends_next_day:
        try:
            resume = datetime.strptime(state.friends_next_day, "%Y-%m-%d").date()
            if resume > start_day:
                start_day = resume
        except ValueError:
            pass

    for day_str in _iter_days(start_day, friends_to):
        _log(f"[friends] day={day_str} types={types} ...")
        try:
            n = await _export_friends_one_day(
                client,
                creds,
                path,
                ctx.rate_gate,
                calendar_day=day_str,
                friend_types=types,
                query_mode=query_mode,
            )
        except Exception as e:
            await ctx.note_error(str(e))
            raise
        async with ctx.state_lock:
            state.friends_rows += n
            d = datetime.strptime(day_str, "%Y-%m-%d").date()
            nxt = d + timedelta(days=1)
            state.friends_next_day = (
                nxt.isoformat() if nxt <= friends_to else friends_to.isoformat()
            )
            if nxt > friends_to:
                state.friends_done = True
                state.friends_next_day = ""
            _save_state(ctx.state_path, state)
        _log(f"[friends] day={day_str} +{n} total={state.friends_rows}")

    async with ctx.state_lock:
        state.friends_done = True
        _save_state(ctx.state_path, state)
    _log(f"[friends] done rows={state.friends_rows}")


async def export_chats(
    client: httpx.AsyncClient,
    creds: OpenCreds,
    ctx: ExportCtx,
    *,
    chat_start_ms: int | None,
    chat_max_calls: int,
) -> None:
    state = ctx.state
    if state.chats_done:
        _log(f"[chats] skip (done, rows={state.chats_rows})")
        return

    path = ctx.out_dir / "chats.jsonl"
    cursor_time = int(state.chat_cursor_time_ms or 0)
    cursor_create = int(state.chat_cursor_create_ts_ms or 0)

    if cursor_time <= 0:
        if chat_start_ms is not None and int(chat_start_ms) > 0:
            cursor_time = int(chat_start_ms)
        else:
            cursor_time = _max_queryable_time_ms() - 365 * 24 * 60 * 60 * 1000
            if cursor_time < 0:
                cursor_time = 0

    max_q = _max_queryable_time_ms()
    if cursor_time > max_q:
        cursor_time = max_q

    unlimited = int(chat_max_calls) <= 0
    calls_left = 10**9 if unlimited else max(1, int(chat_max_calls))
    call_i = 0
    stalled = 0

    while call_i < calls_left:
        call_i += 1
        max_q = _max_queryable_time_ms()
        if cursor_time >= max_q:
            async with ctx.state_lock:
                state.chats_done = True
                _save_state(ctx.state_path, state)
            _log(f"[chats] caught up to queryable max ({max_q})")
            break

        try:
            body = await _open_json_with_retry(
                ctx.rate_gate,
                label=f"chats call={call_i}",
                call=lambda ct=cursor_time, cc=cursor_create: post_all_records(
                    client,
                    creds,
                    timestamp_ms=ct,
                    create_timestamp_ms=cc,
                ),
            )
        except Exception as e:
            await ctx.note_error(str(e))
            raise

        data = body.get("data") or {}
        end_ms = int(data.get("end") or 0) or cursor_time
        cursor_create = int(data.get("createTimestamp") or 0) or 0
        msgs = data.get("messages") or []
        if not isinstance(msgs, list):
            msgs = []

        batch = [m for m in msgs if isinstance(m, dict)]
        n = _append_jsonl(path, batch)
        async with ctx.state_lock:
            state.chats_rows += n
            state.chat_cursor_time_ms = end_ms
            state.chat_cursor_create_ts_ms = cursor_create
            _save_state(ctx.state_path, state)
        _log(
            f"[chats] call={call_i} +{n} total={state.chats_rows} "
            f"cursor={end_ms} createTs={cursor_create}"
        )

        if end_ms <= cursor_time and n == 0:
            stalled += 1
            if stalled >= 3:
                async with ctx.state_lock:
                    state.chats_done = True
                    _save_state(ctx.state_path, state)
                _log("[chats] no progress for 3 calls, mark done")
                break
        else:
            stalled = 0
        cursor_time = end_ms

        if not unlimited and call_i >= calls_left:
            _log(f"[chats] reached --chat-max-calls={chat_max_calls}, pause (not done)")
            break

    if cursor_time >= _max_queryable_time_ms():
        async with ctx.state_lock:
            state.chats_done = True
            _save_state(ctx.state_path, state)

    _log(f"[chats] session end done={state.chats_done} rows={state.chats_rows}")


async def export_voice(
    client: httpx.AsyncClient,
    creds: OpenCreds,
    ctx: ExportCtx,
    *,
    page_size: int,
    voice_max_pages: int,
    call_type: int | None,
    is_room: int | None,
) -> None:
    state = ctx.state
    if state.voice_done:
        _log(f"[voice] skip (done, rows={state.voice_rows})")
        return

    path = ctx.out_dir / "voice.jsonl"
    page_size = max(10, min(int(page_size), 500))
    unlimited = int(voice_max_pages) <= 0
    pages_left = 10**9 if unlimited else max(1, int(voice_max_pages))

    cursor: int | None = None
    if state.voice_started:
        cursor = state.voice_next_id

    page_i = 0
    while page_i < pages_left:
        page_i += 1

        try:
            body = await _open_json_with_retry(
                ctx.rate_gate,
                label=f"voice page={page_i}",
                call=lambda c=cursor: post_voice_page(
                    client,
                    creds,
                    page_size=page_size,
                    next_id=c,
                    call_type=call_type,
                    is_room=is_room,
                ),
            )
        except Exception as e:
            await ctx.note_error(str(e))
            raise

        data = body.get("data") or {}
        items = data.get("data") or []
        if not isinstance(items, list):
            items = []

        if not items:
            async with ctx.state_lock:
                state.voice_done = True
                state.voice_started = True
                _save_state(ctx.state_path, state)
            _log("[voice] empty page, done")
            break

        batch = [it for it in items if isinstance(it, dict)]
        n = _append_jsonl(path, batch)

        last_cursor: int | None = None
        for item in batch:
            try:
                nid = int(item.get("nextId") or 0) or None
            except (TypeError, ValueError):
                nid = None
            if nid is not None:
                last_cursor = nid

        async with ctx.state_lock:
            state.voice_rows += n
            state.voice_started = True
            if last_cursor is not None:
                state.voice_next_id = last_cursor
            _save_state(ctx.state_path, state)
        if last_cursor is not None:
            cursor = last_cursor
        _log(
            f"[voice] page={page_i} +{n} total={state.voice_rows} nextId={state.voice_next_id}"
        )

        if last_cursor is None:
            async with ctx.state_lock:
                state.voice_done = True
                _save_state(ctx.state_path, state)
            _log("[voice] no nextId on page, done")
            break

        if not unlimited and page_i >= pages_left:
            _log(f"[voice] reached --voice-max-pages={voice_max_pages}, pause (not done)")
            break

    _log(f"[voice] session end done={state.voice_done} rows={state.voice_rows}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_entities(s: str) -> list[str]:
    parts = [p.strip().lower() for p in (s or "").split(",") if p.strip()]
    if not parts:
        return list(ALL_ENTITIES)
    bad = [p for p in parts if p not in ALL_ENTITIES]
    if bad:
        raise SystemExit(f"未知 --entities: {bad}；允许: {','.join(ALL_ENTITIES)}")
    return parts


def _parse_day(s: str, name: str) -> date:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError as e:
        raise SystemExit(f"--{name} 须为 YYYY-MM-DD") from e


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="导出云客开放平台数据到 JSONL（不连接当前数据库；实体并行+共享限频）",
    )
    p.add_argument("--out", required=True, help="输出目录（可断点续跑）")
    p.add_argument("--base-url", default=None, help="开放平台 base URL")
    p.add_argument("--company", default=None)
    p.add_argument("--key", default=None)
    p.add_argument("--partner-id", default=None)
    p.add_argument(
        "--entities",
        default="accounts,friends,chats,voice",
        help="逗号分隔: accounts,friends,chats,voice（并行导出，共享 5s 限频）",
    )
    p.add_argument(
        "--friends-from",
        default=None,
        help="好友起始自然日 YYYY-MM-DD（默认：昨天往前 365 天）",
    )
    p.add_argument(
        "--friends-to",
        default=None,
        help="好友截止自然日 YYYY-MM-DD（默认：昨天）",
    )
    p.add_argument(
        "--include-groups",
        action="store_true",
        help="好友接口同时拉取 type=2 群",
    )
    p.add_argument(
        "--friends-query-mode",
        default="updateTime",
        choices=("updateTime", "createTime"),
    )
    p.add_argument(
        "--chat-start-ms",
        type=int,
        default=None,
        help="聊天起始 timestamp(ms)；续跑优先用 state.json",
    )
    p.add_argument(
        "--chat-max-calls",
        type=int,
        default=0,
        help="单次最多 allRecords 调用次数；0=追到可查上限",
    )
    p.add_argument(
        "--voice-max-pages",
        type=int,
        default=0,
        help="单次最多语音分页数；0=拉到空页",
    )
    p.add_argument("--voice-page-size", type=int, default=100)
    p.add_argument(
        "--voice-call-type",
        type=int,
        default=1,
        help="callType，默认 1=语音；传负数表示不传该过滤",
    )
    p.add_argument(
        "--voice-is-room",
        type=int,
        default=0,
        help="isRoom，默认 0=好友；传负数表示不传该过滤",
    )
    p.add_argument("--accounts-page-size", type=int, default=200)
    return p


async def run(args: argparse.Namespace) -> int:
    creds = resolve_creds(
        base_url=args.base_url,
        company=args.company,
        key=args.key,
        partner_id=args.partner_id,
    )
    entities = _parse_entities(args.entities)
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "state.json"
    manifest_path = out_dir / "manifest.json"
    state = _load_state(state_path)

    # 并行一开始就建好空文件，避免「还在跑 friends 时看不见 chats/voice」
    for ent in entities:
        (out_dir / f"{ent}.jsonl").touch(exist_ok=True)

    friends_to = (
        _parse_day(args.friends_to, "friends-to") if args.friends_to else _yesterday_sh()
    )
    friends_from = (
        _parse_day(args.friends_from, "friends-from")
        if args.friends_from
        else _default_friends_from()
    )
    if friends_from > friends_to:
        raise SystemExit("--friends-from 不能晚于 --friends-to")

    started_at = datetime.now(tz=TZ_SH).isoformat()
    manifest: dict[str, Any] = {
        "started_at": started_at,
        "finished_at": None,
        "base_url": creds.base_url,
        "company_masked": _mask(creds.company),
        "partner_id_masked": _mask(creds.partner_id),
        "entities": entities,
        "parallel": True,
        "friends_from": friends_from.isoformat(),
        "friends_to": friends_to.isoformat(),
        "include_groups": bool(args.include_groups),
        "friends_query_mode": args.friends_query_mode,
        "counts": {},
        "ok": False,
        "errors": [],
    }
    _write_manifest(manifest_path, manifest)

    call_type = None if int(args.voice_call_type) < 0 else int(args.voice_call_type)
    is_room = None if int(args.voice_is_room) < 0 else int(args.voice_is_room)

    _log(
        f"export → {out_dir} entities={entities} (parallel, shared {RATE_LIMIT_SEC}s gate) "
        f"company={_mask(creds.company)} partner={_mask(creds.partner_id)}"
    )

    ctx = ExportCtx(
        out_dir=out_dir,
        state=state,
        state_path=state_path,
        rate_gate=_RateGate(RATE_LIMIT_SEC),
    )

    def _fill_manifest(*, ok: bool) -> None:
        manifest["finished_at"] = datetime.now(tz=TZ_SH).isoformat()
        manifest["ok"] = ok and not state.errors
        manifest["errors"] = list(state.errors)
        manifest["counts"] = {
            "accounts": state.accounts_rows,
            "friends": state.friends_rows,
            "chats": state.chats_rows,
            "voice": state.voice_rows,
        }
        manifest["done_flags"] = {
            "accounts": state.accounts_done,
            "friends": state.friends_done,
            "chats": state.chats_done,
            "voice": state.voice_done,
        }
        _write_manifest(manifest_path, manifest)

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            tasks: list[asyncio.Task[None]] = []
            if "accounts" in entities:
                tasks.append(
                    asyncio.create_task(
                        export_accounts(
                            client,
                            creds,
                            ctx,
                            page_size=int(args.accounts_page_size),
                        ),
                        name="export-accounts",
                    )
                )
            if "friends" in entities:
                tasks.append(
                    asyncio.create_task(
                        export_friends(
                            client,
                            creds,
                            ctx,
                            friends_from=friends_from,
                            friends_to=friends_to,
                            include_groups=bool(args.include_groups),
                            query_mode=str(args.friends_query_mode),
                        ),
                        name="export-friends",
                    )
                )
            if "chats" in entities:
                tasks.append(
                    asyncio.create_task(
                        export_chats(
                            client,
                            creds,
                            ctx,
                            chat_start_ms=args.chat_start_ms,
                            chat_max_calls=int(args.chat_max_calls),
                        ),
                        name="export-chats",
                    )
                )
            if "voice" in entities:
                tasks.append(
                    asyncio.create_task(
                        export_voice(
                            client,
                            creds,
                            ctx,
                            page_size=int(args.voice_page_size),
                            voice_max_pages=int(args.voice_max_pages),
                            call_type=call_type,
                            is_room=is_room,
                        ),
                        name="export-voice",
                    )
                )
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failures = [r for r in results if isinstance(r, BaseException)]
            if failures:
                for exc in failures:
                    if not isinstance(exc, Exception):
                        raise exc
                    msg = str(exc)
                    if msg not in state.errors:
                        state.errors.append(msg)
                    _log(f"FAILED task: {exc}")
                await ctx.save_state()
                _fill_manifest(ok=False)
                return 1
    except Exception as e:
        await ctx.note_error(str(e))
        _fill_manifest(ok=False)
        _log(f"FAILED: {e}")
        return 1

    _fill_manifest(ok=True)
    _log(f"OK counts={manifest['counts']} done={manifest['done_flags']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
