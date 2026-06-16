"""
销售微信主数据 sales_wechat_accounts：

- 推荐：开放平台 POST /open/wechat/companyAccounts（见 sync.company_accounts_open）。
- 备用：从 accounts.xlsx（云客导出表头）导入。

表头示例：
  微信ID (wechatId), 账号 (account), 昵称 (nickname), 手机号 (phone), 别名 (alias), ...

用法：
  cd backend && python -m sync.sales_wechat_accounts [xlsx路径]
  未传路径时依次尝试环境变量 ACCOUNTS_XLSX、项目根目录 accounts.xlsx。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.sql import func

from database import AsyncSessionLocal
from models import SalesWechatAccount


def _scalar_str(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int,)):
        return str(v)
    if isinstance(v, float):
        if v == int(v):
            return str(int(v))
        return str(v).strip()
    s = str(v).strip()
    return s or None


def _find_column(df: pd.DataFrame, *candidates: str) -> str | None:
    cols = list(df.columns)
    for c in candidates:
        if c in cols:
            return c
    cnorm = [(str(c).replace(" ", ""), c) for c in cols]
    for cand in candidates:
        t = cand.replace(" ", "")
        for n, orig in cnorm:
            if t.lower() in n.lower():
                return orig
    return None


def rows_from_accounts_xlsx(path: Path) -> list[dict]:
    df = pd.read_excel(path)
    col_id = _find_column(df, "微信ID (wechatId)", "wechatId")
    col_nick = _find_column(df, "昵称 (nickname)", "nickname")
    col_alias = _find_column(df, "别名 (alias)", "alias")
    col_acct = _find_column(df, "账号 (account)", "account")
    col_phone = _find_column(df, "手机号 (phone)", "phone")
    if not col_id:
        raise ValueError(f"未找到微信ID列，当前列: {list(df.columns)}")

    out: list[dict] = []
    for _, s in df.iterrows():
        wid = _scalar_str(s.get(col_id))
        if not wid:
            continue
        row = {
            "sales_wechat_id": wid,
            "nickname": _scalar_str(s.get(col_nick)) if col_nick else None,
            "alias_name": _scalar_str(s.get(col_alias)) if col_alias else None,
            "account_code": _scalar_str(s.get(col_acct)) if col_acct else None,
            "phone": _scalar_str(s.get(col_phone)) if col_phone else None,
        }
        out.append(row)
    return out


async def upsert_rows(rows: list[dict], source: str = "xlsx") -> dict:
    """幂等 upsert，按 sales_wechat_id 更新昵称/别名等。"""
    if not rows:
        return {"upserted": 0}

    async with AsyncSessionLocal() as db:
        n = 0
        for r in rows:
            stmt = mysql_insert(SalesWechatAccount).values(
                sales_wechat_id=r["sales_wechat_id"],
                nickname=r.get("nickname"),
                alias_name=r.get("alias_name"),
                account_code=r.get("account_code"),
                phone=r.get("phone"),
                source=source,
                updated_at=func.now(),
            )
            stmt = stmt.on_duplicate_key_update(
                nickname=stmt.inserted.nickname,
                alias_name=stmt.inserted.alias_name,
                account_code=stmt.inserted.account_code,
                phone=stmt.inserted.phone,
                source=stmt.inserted.source,
                updated_at=func.now(),
            )
            await db.execute(stmt)
            n += 1
        await db.commit()
        return {"upserted": n}


def default_accounts_xlsx_path() -> Path:
    env = (os.environ.get("ACCOUNTS_XLSX") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    root = Path(__file__).resolve().parent.parent.parent
    return (root / "accounts.xlsx").resolve()


def resolve_default_xlsx_path(argv: list[str]) -> Path:
    if len(argv) >= 2 and argv[1].strip():
        return Path(argv[1]).expanduser().resolve()
    return default_accounts_xlsx_path()


async def sync_from_path(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    rows = rows_from_accounts_xlsx(path)
    stats = await upsert_rows(rows, source="xlsx")
    stats["path"] = str(path)
    stats["rows_in_file"] = len(rows)
    return stats


async def _run_and_dispose(path: Path) -> dict:
    try:
        return await sync_from_path(path)
    finally:
        from database import engine

        await engine.dispose()


def main() -> None:
    path = resolve_default_xlsx_path(sys.argv)
    try:
        stats = asyncio.run(_run_and_dispose(path))
        print(stats)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
