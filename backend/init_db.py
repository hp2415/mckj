"""
数据库连通性探测（安全：默认只读，绝不 drop/create）。

用法（在 backend 目录或容器内）:
  python init_db.py
  python init_db.py --timeout 5

默认探测 192.168.0.101:3306/ai_assistant_db（不读本地 .env 的 DATABASE_URL）。
覆盖方式: DB_PROBE_URL / DB_PROBE_HOST / DB_PROBE_PASSWORD 等。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from urllib.parse import quote_plus, urlparse, unquote

from sqlalchemy import create_engine, text

# 本脚本固定测这台；不读本地 .env 的 DATABASE_URL（避免测到 localhost）
DEFAULT_PROBE_HOST = "192.168.0.100"
DEFAULT_PROBE_PORT = 3306
DEFAULT_PROBE_USER = "root"
DEFAULT_PROBE_PASSWORD = "MySQLPassword$"
DEFAULT_PROBE_DB = "ai_assistant_db"


def _build_probe_url() -> str:
    """组装探测 URL：优先完整 DB_PROBE_URL，否则按 host/账号拼。"""
    explicit = (os.getenv("DB_PROBE_URL") or "").strip()
    if explicit:
        return explicit
    host = (os.getenv("DB_PROBE_HOST") or DEFAULT_PROBE_HOST).strip()
    port = int(os.getenv("DB_PROBE_PORT") or DEFAULT_PROBE_PORT)
    user = (os.getenv("DB_PROBE_USER") or DEFAULT_PROBE_USER).strip()
    password = os.getenv("DB_PROBE_PASSWORD")
    if password is None:
        password = DEFAULT_PROBE_PASSWORD
    db = (os.getenv("DB_PROBE_DB") or DEFAULT_PROBE_DB).strip()
    # 密码里可能有 $ 等特殊字符，必须 quote
    return (
        f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{db}"
    )


def _sync_url(async_url: str) -> str:
    """mysql+aiomysql://... -> mysql+pymysql://..."""
    u = (async_url or "").strip()
    if "+aiomysql" in u:
        return u.replace("+aiomysql", "+pymysql", 1)
    if u.startswith("mysql://"):
        return u.replace("mysql://", "mysql+pymysql://", 1)
    return u


def _mask_url(url: str) -> str:
    """日志里隐藏密码。"""
    try:
        p = urlparse(url)
        if not p.password:
            return url
        netloc = p.netloc.replace(f":{p.password}@", ":***@", 1)
        return p._replace(netloc=netloc).geturl()
    except Exception:
        return "<unparseable>"


def _tcp_probe(host: str, port: int, timeout: float) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ms = (time.perf_counter() - t0) * 1000
            return True, ms, "ok"
    except OSError as e:
        ms = (time.perf_counter() - t0) * 1000
        # 110=ETIMEDOUT 丢包; 111=ECONNREFUSED 端口无进程; 113=EHOSTUNREACH
        return False, ms, f"{type(e).__name__}: {e}"


def test_connection(*, timeout: int = 5) -> int:
    url = _sync_url(_build_probe_url())
    parsed = urlparse(url)
    host = parsed.hostname or DEFAULT_PROBE_HOST
    port = parsed.port or DEFAULT_PROBE_PORT
    user = unquote(parsed.username or "")
    db_name = (parsed.path or "/").lstrip("/") or "(default)"

    print("=" * 60)
    print("数据库连通性探测")
    print("=" * 60)
    print(f"  URL      : {_mask_url(url)}")
    print(f"  Host     : {host}:{port}")
    print(f"  User     : {user or '(empty)'}")
    print(f"  Database : {db_name}")
    print(f"  Timeout  : {timeout}s")
    print("-" * 60)

    # 1) 纯 TCP：区分「网络不通」vs「MySQL 鉴权/库名问题」
    ok, tcp_ms, tcp_msg = _tcp_probe(host, port, float(timeout))
    if ok:
        print(f"[1/3] TCP     OK  ({tcp_ms:.0f} ms)")
    else:
        print(f"[1/3] TCP     FAIL ({tcp_ms:.0f} ms) — {tcp_msg}")
        print()
        print("诊断提示:")
        print("  - errno 110 / timed out  → 包被丢弃（防火墙 DROP / docker iptables / 路由）")
        print("  - errno 111 / refused    → 对端无进程监听 3306，或 bind 地址不对")
        print("  - 宿主机能通、容器不通  → 多半是 docker 网络规则被刷掉，试 systemctl restart docker")
        return 1

    # 2) SQLAlchemy + pymysql（与线上同一套 URL，仅驱动不同）
    engine = create_engine(
        url,
        echo=False,
        pool_pre_ping=True,
        connect_args={"connect_timeout": timeout},
    )
    t0 = time.perf_counter()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT "
                    "VERSION() AS ver, "
                    "DATABASE() AS db, "
                    "@@hostname AS host, "
                    "@@port AS port, "
                    "@@wait_timeout AS wait_timeout, "
                    "@@max_connections AS max_conn"
                )
            ).mappings().one()
            threads = "?"
            try:
                st = conn.execute(text("SHOW GLOBAL STATUS LIKE 'Threads_connected'")).first()
                if st is not None:
                    threads = st[1]
            except Exception:
                pass
            sql_ms = (time.perf_counter() - t0) * 1000
            print(f"[2/3] SQL     OK  ({sql_ms:.0f} ms)")
            print(f"        MySQL      : {row['ver']}")
            print(f"        DATABASE() : {row['db']}")
            print(f"        hostname   : {row['host']}:{row['port']}")
            print(f"        wait_timeout / max_connections / Threads_connected = "
                  f"{row['wait_timeout']} / {row['max_conn']} / {threads}")
    except Exception as e:
        sql_ms = (time.perf_counter() - t0) * 1000
        print(f"[2/3] SQL     FAIL ({sql_ms:.0f} ms)")
        print(f"        {type(e).__name__}: {e}")
        print()
        print("诊断提示: TCP 已通但 SQL 失败 → 查账号密码、库名、用户 host 权限、SSL。")
        engine.dispose()
        return 2

    # 3) 池参数回显（与 database.py 对齐，便于对照线上配置）
    def _env_int(key: str, default: int) -> int:
        try:
            return max(1, int(str(os.getenv(key) or default).strip()))
        except ValueError:
            return default

    print("[3/3] 应用侧池配置（database.py / 环境变量）")
    print(f"        pool_size={_env_int('DB_POOL_SIZE', 20)}  "
          f"max_overflow={_env_int('DB_MAX_OVERFLOW', 10)}  "
          f"pool_timeout={_env_int('DB_POOL_TIMEOUT', 10)}  "
          f"connect_timeout={_env_int('DB_CONNECT_TIMEOUT', 5)}  "
          f"pool_recycle={_env_int('DB_POOL_RECYCLE', 280)}")
    print("=" * 60)
    print("结果: 连通正常")
    engine.dispose()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="探测 MySQL 连通性（只读，不改表）")
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("DB_CONNECT_TIMEOUT") or "5"),
        help="TCP/SQL 连接超时秒数（默认 DB_CONNECT_TIMEOUT 或 5）",
    )
    args = parser.parse_args()
    code = test_connection(timeout=max(1, args.timeout))
    sys.exit(code)


if __name__ == "__main__":
    main()
