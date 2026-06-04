"""
一次性迁移：wechat_voice.wechat_voice → raw_wechat_voice_calls

源库来自旧脚本 D:\\D\\wechat_voice_script 同步结果，默认库名 wechat_voice。
目标库为当前应用 DATABASE_URL 所指库。

迁移完成后会将 system_configs.wechat_voice_cursor_next_id 设为源表 MAX(next_id)，
以便开放平台增量同步从旧游标继续。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote, urlparse

import pymysql
from dotenv import load_dotenv

from core.wechat_voice_sync import CFG_VOICE_CURSOR, CFG_VOICE_LAST_MSG, CFG_VOICE_LAST_OK

load_dotenv()

_LEGACY_INSERT_SQL = """
INSERT INTO `{target_db}`.raw_wechat_voice_calls (
    record_id,
    user_name,
    user_phone,
    user_we_chat_nick_name,
    user_we_chat_alias,
    user_we_chat_head_img,
    user_we_chat_phone,
    talker_head_img,
    talker_nick_name,
    talker_alias,
    call_type,
    is_send,
    call_status,
    oss_file_name,
    duration,
    start_time,
    end_time,
    we_chat_id,
    talker,
    is_room,
    remark,
    duration_file,
    cursor_next_id,
    user_id,
    raw_json,
    imported_at
)
SELECT
    src.id,
    NULLIF(src.user_name, ''),
    NULLIF(src.user_phone, ''),
    NULLIF(src.user_we_chat_nick_name, ''),
    NULLIF(src.user_we_chat_alias, ''),
    src.user_we_chat_head_img,
    src.user_we_chat_phone,
    src.talker_head_img,
    src.talker_nick_name,
    src.talker_alias,
    src.call_type,
    src.is_send,
    src.call_status,
    src.oss_file_name,
    NULLIF(src.duration, ''),
    src.start_time,
    src.end_time,
    src.we_chat_id,
    src.talker,
    src.is_room,
    src.remark,
    src.duration_file,
    src.next_id,
    src.user_id,
    NULL,
    src.created_at
FROM `{source_db}`.wechat_voice AS src
ON DUPLICATE KEY UPDATE
    user_name = VALUES(user_name),
    user_phone = VALUES(user_phone),
    user_we_chat_nick_name = VALUES(user_we_chat_nick_name),
    user_we_chat_alias = VALUES(user_we_chat_alias),
    user_we_chat_head_img = VALUES(user_we_chat_head_img),
    user_we_chat_phone = VALUES(user_we_chat_phone),
    talker_head_img = VALUES(talker_head_img),
    talker_nick_name = VALUES(talker_nick_name),
    talker_alias = VALUES(talker_alias),
    call_type = VALUES(call_type),
    is_send = VALUES(is_send),
    call_status = VALUES(call_status),
    oss_file_name = VALUES(oss_file_name),
    duration = VALUES(duration),
    start_time = VALUES(start_time),
    end_time = VALUES(end_time),
    we_chat_id = VALUES(we_chat_id),
    talker = VALUES(talker),
    is_room = VALUES(is_room),
    remark = VALUES(remark),
    duration_file = VALUES(duration_file),
    cursor_next_id = VALUES(cursor_next_id),
    user_id = VALUES(user_id),
    imported_at = VALUES(imported_at)
"""


@dataclass
class LegacyVoiceImportResult:
    source_db: str
    target_db: str
    source_rows: int
    target_rows_before: int
    target_rows_after: int
    max_next_id: int | None
    inserted_or_updated: int
    cursor_written: bool


def _parse_mysql_url(url: str) -> dict[str, str | int]:
    u = (url or "").strip()
    u = re.sub(r"^mysql\+aiomysql://", "mysql://", u, flags=re.I)
    u = re.sub(r"^mysql\+pymysql://", "mysql://", u, flags=re.I)
    p = urlparse(u)
    db = (p.path or "").lstrip("/").split("?")[0]
    return {
        "host": p.hostname or "localhost",
        "port": int(p.port or 3306),
        "user": unquote(p.username or "root"),
        "password": unquote(p.password or ""),
        "database": db,
    }


def _connect(**kwargs) -> pymysql.connections.Connection:
    return pymysql.connect(charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, **kwargs)


def _upsert_system_config(cur, key: str, value: str, group: str = "sync") -> None:
    cur.execute(
        "SELECT id FROM system_configs WHERE config_key=%s LIMIT 1",
        (key,),
    )
    row = cur.fetchone()
    now = datetime.now()
    if row:
        cur.execute(
            "UPDATE system_configs SET config_value=%s, config_group=%s, updated_at=%s WHERE config_key=%s",
            (value, group, now, key),
        )
    else:
        cur.execute(
            """
            INSERT INTO system_configs (config_key, config_value, config_group, updated_at)
            VALUES (%s, %s, %s, %s)
            """,
            (key, value, group, now),
        )


def migrate_legacy_wechat_voice_table(
    *,
    source_db: str | None = None,
    dry_run: bool = False,
    write_cursor: bool = True,
) -> LegacyVoiceImportResult:
    target_cfg = _parse_mysql_url(os.getenv("DATABASE_URL", ""))
    if not target_cfg["database"]:
        raise ValueError("DATABASE_URL 未配置或缺少库名")

    source_db_name = (source_db or os.getenv("WECHAT_VOICE_LEGACY_DATABASE") or "wechat_voice").strip()
    conn = _connect(
        host=target_cfg["host"],
        port=target_cfg["port"],
        user=target_cfg["user"],
        password=target_cfg["password"],
        database=str(target_cfg["database"]),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM `{source_db_name}`.wechat_voice")
            source_rows = int((cur.fetchone() or {}).get("n") or 0)

            cur.execute("SELECT COUNT(*) AS n FROM raw_wechat_voice_calls")
            before = int((cur.fetchone() or {}).get("n") or 0)

            cur.execute(f"SELECT MAX(next_id) AS m FROM `{source_db_name}`.wechat_voice")
            max_next_raw = (cur.fetchone() or {}).get("m")
            max_next_id = int(max_next_raw) if max_next_raw is not None else None

            if dry_run:
                return LegacyVoiceImportResult(
                    source_db=source_db_name,
                    target_db=str(target_cfg["database"]),
                    source_rows=source_rows,
                    target_rows_before=before,
                    target_rows_after=before,
                    max_next_id=max_next_id,
                    inserted_or_updated=0,
                    cursor_written=False,
                )

            sql = _LEGACY_INSERT_SQL.format(
                target_db=str(target_cfg["database"]),
                source_db=source_db_name,
            )
            affected = cur.execute(sql)
            conn.commit()

            cur.execute("SELECT COUNT(*) AS n FROM raw_wechat_voice_calls")
            after = int((cur.fetchone() or {}).get("n") or 0)

            cursor_written = False
            if write_cursor and max_next_id is not None:
                _upsert_system_config(cur, CFG_VOICE_CURSOR, str(max_next_id), "sync")
                msg = (
                    f"legacy import from {source_db_name}.wechat_voice: "
                    f"source={source_rows} target={before}->{after} max_next_id={max_next_id}"
                )
                _upsert_system_config(cur, CFG_VOICE_LAST_MSG, msg[:2000], "sync")
                _upsert_system_config(cur, CFG_VOICE_LAST_OK, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "sync")
                conn.commit()
                cursor_written = True

            return LegacyVoiceImportResult(
                source_db=source_db_name,
                target_db=str(target_cfg["database"]),
                source_rows=source_rows,
                target_rows_before=before,
                target_rows_after=after,
                max_next_id=max_next_id,
                inserted_or_updated=int(affected or 0),
                cursor_written=cursor_written,
            )
    finally:
        conn.close()
