"""
将云客导出 JSONL（accounts/friends/chats/voice）幂等导入**外部指定**的 MySQL 库。

不使用项目 database.py / .env 的 DATABASE_URL；连接仅来自：
  - CLI: --database-url
  - 或 CLI: --db-host/--db-port/--db-user/--db-password/--db-name
  - 或环境变量: YUNKE_IMPORT_DATABASE_URL
  - 或环境变量: YUNKE_IMPORT_DB_HOST / PORT / USER / PASSWORD / NAME

默认读取本目录下样例文件：
  sync/accounts.jsonl / friends.jsonl / chats.jsonl / voice.jsonl

用法：
  cd backend
  python -m sync.import_yunke_jsonl \\
    --database-url "mysql+aiomysql://user:pass@host:3306/dbname"

  python -m sync.import_yunke_jsonl \\
    --db-host 1.2.3.4 --db-port 3306 --db-user root --db-password xxx --db-name ai_assistant_db \\
    --dir D:/exports/yunke_other_acct

导入范围：
  --entities friends
  --entities friends accounts
  --entities friends,accounts,chats
  不传 --entities 时导入 accounts/friends/chats/voice 全部。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote_plus

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import func as sqlfunc

from ai.chat_log_filter import is_noise_chat_text
from core.wechat_chat_sync import _normalize_row_from_message
from core.wechat_friends_sync import (
    _map_item_to_rc_fields,
    _merge_raw_customer,
    _upsert_rcsw,
)
from core.wechat_voice_sync import _normalize_voice_row
from models import (
    RawChatLog,
    RawCustomer,
    RawCustomerSalesWechat,
    RawWechatVoiceCall,
    SalesWechatAccount,
)

ALL_ENTITIES = ("accounts", "friends", "chats", "voice")
CHAT_BATCH = 50
VOICE_BATCH = 100
FRIENDS_BATCH = 200
SOURCE = "yunke_jsonl_import"

SessionFactory = Callable[[], Any]


def _log(msg: str) -> None:
    print(msg, flush=True)


def _default_dir() -> Path:
    return Path(__file__).resolve().parent


def _mask_db_url(url: str) -> str:
    """Hide password in mysql+aiomysql://user:pass@host/db."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    creds, hostpart = rest.rsplit("@", 1)
    if ":" in creds:
        user, _pw = creds.split(":", 1)
        return f"{scheme}://{user}:***@{hostpart}"
    return f"{scheme}://***@{hostpart}"


def resolve_database_url(args: argparse.Namespace) -> str:
    """仅接受外部连接配置，不回退项目 DATABASE_URL。"""
    url = (args.database_url or "").strip() or (os.getenv("YUNKE_IMPORT_DATABASE_URL") or "").strip()
    if url:
        if url.startswith("mysql://"):
            url = "mysql+aiomysql://" + url[len("mysql://") :]
        return url

    host = (args.db_host or "").strip() or (os.getenv("YUNKE_IMPORT_DB_HOST") or "").strip()
    user = (args.db_user or "").strip() or (os.getenv("YUNKE_IMPORT_DB_USER") or "").strip()
    password = args.db_password if args.db_password is not None else (os.getenv("YUNKE_IMPORT_DB_PASSWORD") or "")
    name = (args.db_name or "").strip() or (os.getenv("YUNKE_IMPORT_DB_NAME") or "").strip()
    port = (args.db_port or "").strip() or (os.getenv("YUNKE_IMPORT_DB_PORT") or "3306").strip() or "3306"

    missing = [k for k, v in (("host", host), ("user", user), ("name", name)) if not v]
    if missing:
        raise SystemExit(
            "缺少外部数据库连接：请传 --database-url，或 "
            "--db-host/--db-user/--db-password/--db-name，"
            "或设置 YUNKE_IMPORT_DATABASE_URL / YUNKE_IMPORT_DB_*。"
            f" 当前缺: {', '.join(missing)}"
        )

    return (
        f"mysql+aiomysql://{quote_plus(user)}:{quote_plus(str(password))}"
        f"@{host}:{port}/{name}"
    )


def create_external_engine(database_url: str) -> tuple[AsyncEngine, sessionmaker]:
    engine = create_async_engine(
        database_url,
        echo=False,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=280,
        connect_args={"connect_timeout": 10},
    )
    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_factory


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path.name}:{line_no} JSON 解析失败: {e}") from e
            if isinstance(obj, dict):
                yield obj


def _parse_entities(values: str | list[str] | None) -> list[str]:
    """支持单值、空格分隔和逗号分隔，并保持用户指定顺序。"""
    raw_values = [values] if isinstance(values, str) else (values or [])
    parts: list[str] = []
    for value in raw_values:
        for part in value.split(","):
            entity = part.strip().lower()
            if entity and entity not in parts:
                parts.append(entity)
    if not parts:
        return list(ALL_ENTITIES)
    bad = [p for p in parts if p not in ALL_ENTITIES]
    if bad:
        raise SystemExit(f"未知 --entities: {bad}；允许: {','.join(ALL_ENTITIES)}")
    return parts


async def import_accounts(session_factory: sessionmaker, path: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for obj in _iter_jsonl(path):
        wid = str(obj.get("sales_wechat_id") or "").strip()
        if not wid:
            continue
        rows.append(
            {
                "sales_wechat_id": wid,
                "nickname": obj.get("nickname"),
                "alias_name": obj.get("alias_name"),
                "account_code": obj.get("account_code"),
                "phone": obj.get("phone"),
            }
        )
    if not rows:
        return {"upserted": 0, "file_rows": 0, "sample_ids": []}

    upserted = 0
    async with session_factory() as db:
        for r in rows:
            stmt = mysql_insert(SalesWechatAccount).values(
                sales_wechat_id=r["sales_wechat_id"],
                nickname=r.get("nickname"),
                alias_name=r.get("alias_name"),
                account_code=r.get("account_code"),
                phone=r.get("phone"),
                source=SOURCE,
                updated_at=sqlfunc.now(),
            )
            stmt = stmt.on_duplicate_key_update(
                nickname=stmt.inserted.nickname,
                alias_name=stmt.inserted.alias_name,
                account_code=stmt.inserted.account_code,
                phone=stmt.inserted.phone,
                source=stmt.inserted.source,
                updated_at=sqlfunc.now(),
            )
            await db.execute(stmt)
            upserted += 1
        await db.commit()

    return {
        "upserted": upserted,
        "file_rows": len(rows),
        "sample_ids": [r["sales_wechat_id"] for r in rows[:5]],
    }


async def import_friends(session_factory: sessionmaker, path: Path) -> dict[str, Any]:
    items = list(_iter_jsonl(path))
    applied = 0
    sample_ids: list[str] = []

    async with session_factory() as db:
        batch: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item in items:
            raw = {k: v for k, v in item.items() if not str(k).startswith("_export_")}
            fields = _map_item_to_rc_fields(raw)
            if not fields["id"] or not fields["sales_wechat_id"]:
                continue
            batch.append((raw, fields))
            if len(sample_ids) < 5:
                sample_ids.append(fields["id"])

            if len(batch) >= FRIENDS_BATCH:
                applied += await _flush_friends_batch(db, batch)
                batch = []
        if batch:
            applied += await _flush_friends_batch(db, batch)

    return {"file_rows": len(items), "upserted": applied, "sample_ids": sample_ids}


async def _flush_friends_batch(
    db,
    batch: list[tuple[dict[str, Any], dict[str, Any]]],
) -> int:
    rc_ids = list({f["id"] for _, f in batch})
    rc_rows = (await db.execute(select(RawCustomer).where(RawCustomer.id.in_(rc_ids)))).scalars().all()
    rc_map: dict[str, RawCustomer] = {r.id: r for r in rc_rows}
    rcsw_rows = (
        (
            await db.execute(
                select(RawCustomerSalesWechat).where(
                    RawCustomerSalesWechat.raw_customer_id.in_(rc_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    rcsw_map: dict[tuple[str, str], RawCustomerSalesWechat] = {
        (r.raw_customer_id, r.sales_wechat_id): r for r in rcsw_rows
    }
    for item, fields in batch:
        _merge_raw_customer(db, fields, rc_map)
        _upsert_rcsw(db, item, fields, rcsw_map)
    await db.commit()
    return len(batch)


async def import_chats(session_factory: sessionmaker, path: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    skipped_noise = 0
    skipped_key = 0
    sample_keys: list[str] = []

    for m in _iter_jsonl(path):
        row = _normalize_row_from_message(m)
        if not row["wechat_id"] or not row["talker"] or not row["msg_svr_id"]:
            skipped_key += 1
            continue
        if is_noise_chat_text(row.get("text")):
            skipped_noise += 1
            continue
        rows.append(row)
        if len(sample_keys) < 5:
            sample_keys.append(f"{row['wechat_id']}|{row['talker']}|{row['msg_svr_id']}")

    upserted = 0
    async with session_factory() as db:
        for start in range(0, len(rows), CHAT_BATCH):
            chunk = rows[start : start + CHAT_BATCH]
            stmt = mysql_insert(RawChatLog).values(chunk)
            stmt = stmt.on_duplicate_key_update(
                roomid=stmt.inserted.roomid,
                text=stmt.inserted.text,
                raw_json=stmt.inserted.raw_json,
                send_timestamp_ms=stmt.inserted.send_timestamp_ms,
                time_ms=stmt.inserted.time_ms,
                timestamp=stmt.inserted.timestamp,
                is_send=stmt.inserted.is_send,
                message_type=stmt.inserted.message_type,
                file_source=stmt.inserted.file_source,
                imported_at=stmt.inserted.imported_at,
            )
            await db.execute(stmt)
            await db.commit()
            upserted += len(chunk)

    return {
        "file_rows": upserted + skipped_noise + skipped_key,
        "upserted": upserted,
        "skipped_noise": skipped_noise,
        "skipped_key": skipped_key,
        "sample_keys": sample_keys,
    }


async def import_voice(session_factory: sessionmaker, path: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    skipped = 0
    sample_ids: list[str] = []
    for item in _iter_jsonl(path):
        row = _normalize_voice_row(item)
        if row is None:
            skipped += 1
            continue
        rows.append(row)
        if len(sample_ids) < 5:
            sample_ids.append(str(row["record_id"]))

    upserted = 0
    async with session_factory() as db:
        for start in range(0, len(rows), VOICE_BATCH):
            chunk = rows[start : start + VOICE_BATCH]
            stmt = mysql_insert(RawWechatVoiceCall).values(chunk)
            stmt = stmt.on_duplicate_key_update(
                user_name=stmt.inserted.user_name,
                user_phone=stmt.inserted.user_phone,
                user_we_chat_nick_name=stmt.inserted.user_we_chat_nick_name,
                user_we_chat_alias=stmt.inserted.user_we_chat_alias,
                user_we_chat_head_img=stmt.inserted.user_we_chat_head_img,
                user_we_chat_phone=stmt.inserted.user_we_chat_phone,
                talker_head_img=stmt.inserted.talker_head_img,
                talker_nick_name=stmt.inserted.talker_nick_name,
                talker_alias=stmt.inserted.talker_alias,
                call_type=stmt.inserted.call_type,
                is_send=stmt.inserted.is_send,
                call_status=stmt.inserted.call_status,
                oss_file_name=stmt.inserted.oss_file_name,
                duration=stmt.inserted.duration,
                start_time=stmt.inserted.start_time,
                end_time=stmt.inserted.end_time,
                we_chat_id=stmt.inserted.we_chat_id,
                talker=stmt.inserted.talker,
                is_room=stmt.inserted.is_room,
                remark=stmt.inserted.remark,
                duration_file=stmt.inserted.duration_file,
                cursor_next_id=stmt.inserted.cursor_next_id,
                user_id=stmt.inserted.user_id,
                raw_json=stmt.inserted.raw_json,
                imported_at=stmt.inserted.imported_at,
            )
            await db.execute(stmt)
            await db.commit()
            upserted += len(chunk)

    return {
        "file_rows": upserted + skipped,
        "upserted": upserted,
        "skipped": skipped,
        "sample_ids": sample_ids,
    }


async def verify_import(session_factory: sessionmaker, stats: dict[str, Any]) -> dict[str, Any]:
    """按样例主键回查外部库，证明写入成功。"""
    out: dict[str, Any] = {}
    async with session_factory() as db:
        if "accounts" in stats:
            ids = stats["accounts"].get("sample_ids") or []
            if ids:
                n = (
                    await db.execute(
                        select(func.count())
                        .select_from(SalesWechatAccount)
                        .where(SalesWechatAccount.sales_wechat_id.in_(ids))
                    )
                ).scalar_one()
                src = (
                    await db.execute(
                        select(func.count())
                        .select_from(SalesWechatAccount)
                        .where(SalesWechatAccount.source == SOURCE)
                    )
                ).scalar_one()
                out["accounts"] = {
                    "sample_found": int(n),
                    "sample_expected": len(ids),
                    "rows_with_source": int(src),
                    "ok": int(n) == len(ids),
                }

        if "friends" in stats:
            ids = stats["friends"].get("sample_ids") or []
            if ids:
                n_rc = (
                    await db.execute(
                        select(func.count()).select_from(RawCustomer).where(RawCustomer.id.in_(ids))
                    )
                ).scalar_one()
                n_rcsw = (
                    await db.execute(
                        select(func.count())
                        .select_from(RawCustomerSalesWechat)
                        .where(RawCustomerSalesWechat.raw_customer_id.in_(ids))
                    )
                ).scalar_one()
                out["friends"] = {
                    "raw_customers_found": int(n_rc),
                    "rcsw_found": int(n_rcsw),
                    "sample_expected": len(ids),
                    "ok": int(n_rc) == len(ids) and int(n_rcsw) >= len(ids),
                }

        if "chats" in stats:
            keys = stats["chats"].get("sample_keys") or []
            found = 0
            for key in keys:
                parts = key.split("|", 2)
                if len(parts) != 3:
                    continue
                wid, talker, msg = parts
                exists = (
                    await db.execute(
                        select(RawChatLog.id)
                        .where(
                            RawChatLog.wechat_id == wid,
                            RawChatLog.talker == talker,
                            RawChatLog.msg_svr_id == msg,
                        )
                        .limit(1)
                    )
                ).first()
                if exists:
                    found += 1
            out["chats"] = {
                "sample_found": found,
                "sample_expected": len(keys),
                "ok": found == len(keys) and len(keys) > 0,
            }

        if "voice" in stats:
            ids = stats["voice"].get("sample_ids") or []
            if ids:
                n = (
                    await db.execute(
                        select(func.count())
                        .select_from(RawWechatVoiceCall)
                        .where(RawWechatVoiceCall.record_id.in_(ids))
                    )
                ).scalar_one()
                out["voice"] = {
                    "sample_found": int(n),
                    "sample_expected": len(ids),
                    "ok": int(n) == len(ids),
                }

        out["totals"] = {
            "sales_wechat_accounts": int(
                (await db.execute(select(func.count()).select_from(SalesWechatAccount))).scalar_one()
            ),
            "raw_customers": int(
                (await db.execute(select(func.count()).select_from(RawCustomer))).scalar_one()
            ),
            "raw_customer_sales_wechats": int(
                (
                    await db.execute(select(func.count()).select_from(RawCustomerSalesWechat))
                ).scalar_one()
            ),
            "raw_chat_logs": int(
                (await db.execute(select(func.count()).select_from(RawChatLog))).scalar_one()
            ),
            "raw_wechat_voice_calls": int(
                (
                    await db.execute(select(func.count()).select_from(RawWechatVoiceCall))
                ).scalar_one()
            ),
        }
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="导入云客 JSONL 到外部 MySQL（不使用项目 DATABASE_URL）")
    p.add_argument(
        "--dir",
        default=None,
        help="含 accounts/friends/chats/voice.jsonl 的目录（默认：sync/）",
    )
    p.add_argument(
        "--entities",
        nargs="+",
        default=list(ALL_ENTITIES),
        metavar="ENTITY",
        help=(
            "要导入的实体，可传一个或多个；支持空格或逗号分隔。"
            "可选: accounts friends chats voice；默认全部"
        ),
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="外部库 URL，如 mysql+aiomysql://user:pass@host:3306/db（优先于分段参数）",
    )
    p.add_argument("--db-host", default=None, help="外部库 host")
    p.add_argument("--db-port", default=None, help="外部库端口，默认 3306")
    p.add_argument("--db-user", default=None, help="外部库用户")
    p.add_argument("--db-password", default=None, help="外部库密码")
    p.add_argument("--db-name", default=None, help="外部库名")
    p.add_argument(
        "--verify",
        action="store_true",
        default=True,
        help="导入后按样例主键回查（默认开启）",
    )
    p.add_argument(
        "--no-verify",
        action="store_true",
        help="跳过回查",
    )
    return p


async def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.dir).expanduser().resolve() if args.dir else _default_dir()
    entities = _parse_entities(args.entities)
    do_verify = bool(args.verify) and not bool(args.no_verify)
    database_url = resolve_database_url(args)

    _log(f"import from {out_dir} entities={entities}")
    _log(f"external db {_mask_db_url(database_url)}")

    engine, session_factory = create_external_engine(database_url)
    stats: dict[str, Any] = {}
    try:
        # 先探活
        async with session_factory() as db:
            await db.execute(select(1))

        if "accounts" in entities:
            p = out_dir / "accounts.jsonl"
            if not p.is_file():
                _log(f"[accounts] missing {p}")
            else:
                s = await import_accounts(session_factory, p)
                stats["accounts"] = s
                _log(f"[accounts] upserted={s.get('upserted')} file_rows={s.get('file_rows')}")

        if "friends" in entities:
            p = out_dir / "friends.jsonl"
            if not p.is_file():
                _log(f"[friends] missing {p}")
            else:
                s = await import_friends(session_factory, p)
                stats["friends"] = s
                _log(f"[friends] upserted={s.get('upserted')} file_rows={s.get('file_rows')}")

        if "chats" in entities:
            p = out_dir / "chats.jsonl"
            if not p.is_file():
                _log(f"[chats] missing {p}")
            else:
                s = await import_chats(session_factory, p)
                stats["chats"] = s
                _log(
                    f"[chats] upserted={s.get('upserted')} "
                    f"noise={s.get('skipped_noise')} bad_key={s.get('skipped_key')}"
                )

        if "voice" in entities:
            p = out_dir / "voice.jsonl"
            if not p.is_file():
                _log(f"[voice] missing {p}")
            else:
                s = await import_voice(session_factory, p)
                stats["voice"] = s
                _log(
                    f"[voice] upserted={s.get('upserted')} skipped={s.get('skipped')} "
                    f"file_rows={s.get('file_rows')}"
                )

        if do_verify and stats:
            v = await verify_import(session_factory, stats)
            _log("verify=" + json.dumps(v, ensure_ascii=False, indent=2))
            entity_oks = [x.get("ok") for k, x in v.items() if k != "totals" and isinstance(x, dict)]
            if entity_oks and not all(entity_oks):
                _log("VERIFY FAILED")
                return 2
            _log("VERIFY OK")
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
