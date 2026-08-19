"""上传表格的大小、类型与行数限制。"""
from __future__ import annotations

import io

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_ROWS = 20_000
_READ_CHUNK = 64 * 1024

_XLS_MAGIC = b"\xd0\xcf\x11\xe0"


class UploadLimitError(ValueError):
    pass


async def read_capped_upload(upload, *, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    read = getattr(upload, "read", None)
    if not callable(read):
        raise UploadLimitError("无效的上传文件")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UploadLimitError(f"文件过大，最大允许 {max_bytes // (1024 * 1024)}MB")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise UploadLimitError("上传文件为空")
    return data


def detect_spreadsheet_kind(
    filename: str,
    content: bytes,
    *,
    kinds: tuple[str, ...] = ("csv", "xlsx"),
) -> str:
    name = (filename or "").strip().lower()
    allowed = tuple(k.lower() for k in kinds)
    if name.endswith(".csv") and "csv" in allowed:
        if b"\x00" in content[:8192]:
            raise UploadLimitError("CSV 文件内容无效")
        return "csv"
    if name.endswith(".xlsx") and "xlsx" in allowed:
        if not content.startswith(b"PK"):
            raise UploadLimitError("不是有效的 .xlsx 文件")
        return "xlsx"
    if name.endswith(".xls") and "xls" in allowed:
        if not content.startswith(_XLS_MAGIC):
            raise UploadLimitError("不是有效的 .xls 文件")
        return "xls"
    labels = " / ".join(f".{k}" for k in allowed)
    raise UploadLimitError(f"仅支持 {labels} 格式文件")


def load_spreadsheet_df(kind: str, content: bytes, *, max_rows: int = MAX_UPLOAD_ROWS):
    import pandas as pd

    bio = io.BytesIO(content)
    try:
        if kind == "csv":
            df = pd.read_csv(bio, nrows=max_rows + 1)
        else:
            df = pd.read_excel(bio, nrows=max_rows + 1)
    except UploadLimitError:
        raise
    except Exception as exc:
        raise UploadLimitError(f"文件解析失败: {exc}") from exc
    if len(df.index) > max_rows:
        raise UploadLimitError(f"行数超过 {max_rows}，请拆分后重试")
    return df
