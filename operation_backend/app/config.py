"""运营后台配置。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_APP_DIR = Path(__file__).resolve().parent
_ROOT = _APP_DIR.parent

# 优先 operation_backend/.env，其次仓库根 / backend/.env（方便本地共用 SECRET_KEY）
load_dotenv(_ROOT / ".env", override=False)
load_dotenv(_ROOT.parent / "backend" / ".env", override=False)
load_dotenv(_ROOT.parent / ".env", override=False)


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _int(key: str, default: int) -> int:
    try:
        return int(str(os.getenv(key) or default).strip())
    except ValueError:
        return default


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+aiomysql://root:root@localhost:3306/ai_assistant_db",
)

SECRET_KEY = os.getenv("SECRET_KEY") or ""
if not SECRET_KEY:
    raise RuntimeError("环境变量 SECRET_KEY 未配置！请在 operation_backend/.env 中设置。")

ALGORITHM = "HS256"
JWT_AUDIENCE = "operation"
JWT_ISSUER = "mibuddy-operation"
OP_JWT_EXPIRE_HOURS = max(1, _int("OP_JWT_EXPIRE_HOURS", 12))

ENABLE_API_DOCS = _truthy(os.getenv("ENABLE_API_DOCS"))
OP_STAFF_LOGIN_ENABLED = _truthy(os.getenv("OP_STAFF_LOGIN_ENABLED"))

OP_INVITE_DEFAULT_DAYS = max(1, _int("OP_INVITE_DEFAULT_DAYS", 7))
OP_INVITE_DEFAULT_MAX_USES = max(1, _int("OP_INVITE_DEFAULT_MAX_USES", 1))

DB_POOL_SIZE = max(1, _int("DB_POOL_SIZE", 5))
DB_MAX_OVERFLOW = max(0, _int("DB_MAX_OVERFLOW", 5))

CORS_ALLOW_ORIGINS = [
    o.strip()
    for o in str(os.getenv("CORS_ALLOW_ORIGINS") or "").split(",")
    if o.strip() and o.strip() != "*"
]

WEB_DIST = _ROOT / "web" / "dist"
