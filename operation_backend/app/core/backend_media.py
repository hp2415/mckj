"""通过核心 backend 内部 API 读写活动海报（跨容器部署）。

上传/删除：走 /api/internal/* + X-Internal-Key。
浏览器展示：与桌面一致，使用 BACKEND_PUBLIC_URL + /media/campaigns/...
未配置 BACKEND_BASE_URL 时回退本地 MEDIA_DIR（同机开发）。
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path

import httpx

from app.config import BACKEND_BASE_URL, BACKEND_PUBLIC_URL, INTERNAL_API_KEY
from app.core.campaign_media import (
    CampaignMediaError,
    delete_campaign_dir as local_delete_campaign_dir,
    delete_poster_file as local_delete_poster_file,
    media_root,
    save_campaign_poster as local_save_campaign_poster,
)

_CAMPAIGN_PATH_RE = re.compile(
    r"^/media/campaigns/(?P<cid>\d+)/(?P<filename>[^/]+)$"
)


def use_backend_media_api() -> bool:
    return bool(BACKEND_BASE_URL and INTERNAL_API_KEY)


def public_media_base() -> str:
    """浏览器可访问的 backend 根（与桌面 base_url 同类）。"""
    return BACKEND_PUBLIC_URL or BACKEND_BASE_URL or ""


def absolute_media_url(image_path: str | None) -> str | None:
    """把 /media/... 转成可给 <img> 用的绝对地址；无 backend 时返回原相对路径。"""
    rel = (image_path or "").strip().replace("\\", "/")
    if not rel:
        return None
    if rel.startswith("http://") or rel.startswith("https://"):
        return rel
    if not rel.startswith("/"):
        rel = "/" + rel
    base = public_media_base()
    if base:
        return f"{base}{rel}"
    return rel


def _headers() -> dict[str, str]:
    return {"X-Internal-Key": INTERNAL_API_KEY}


def parse_campaign_image_path(image_path: str) -> tuple[int, str] | None:
    rel = (image_path or "").strip().replace("\\", "/")
    m = _CAMPAIGN_PATH_RE.match(rel)
    if not m:
        return None
    return int(m.group("cid")), m.group("filename")


def _local_abs_path(image_path: str) -> Path | None:
    parsed = parse_campaign_image_path(image_path)
    if not parsed:
        return None
    cid, filename = parsed
    p = (media_root() / "campaigns" / str(cid) / filename).resolve()
    try:
        p.relative_to((media_root() / "campaigns").resolve())
    except ValueError:
        return None
    return p if p.is_file() else None


async def upload_campaign_poster(
    campaign_id: int, filename: str, content: bytes
) -> str:
    if not content:
        raise CampaignMediaError("上传文件为空")
    if use_backend_media_api():
        url = f"{BACKEND_BASE_URL}/api/internal/campaigns/{int(campaign_id)}/posters"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url,
                headers=_headers(),
                files={
                    "files": (
                        filename or "poster.jpg",
                        content,
                        "application/octet-stream",
                    )
                },
            )
        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = body.get("message") or body.get("detail") or resp.text
            except Exception:
                msg = resp.text or f"HTTP {resp.status_code}"
            raise CampaignMediaError(str(msg))
        data = resp.json()
        items = (data.get("data") or {}).get("items") or data.get("items") or []
        if not items:
            raise CampaignMediaError("backend 未返回海报路径")
        path = str(items[0].get("image_path") or "").strip()
        if not path:
            raise CampaignMediaError("backend 返回空海报路径")
        return path
    return local_save_campaign_poster(campaign_id, filename, content)


async def delete_poster_media(image_path: str) -> None:
    parsed = parse_campaign_image_path(image_path)
    if not parsed:
        return
    cid, filename = parsed
    if use_backend_media_api():
        url = f"{BACKEND_BASE_URL}/api/internal/media/campaigns/{cid}/{filename}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(url, headers=_headers())
        if resp.status_code >= 400 and resp.status_code != 404:
            try:
                body = resp.json()
                msg = body.get("message") or body.get("detail") or resp.text
            except Exception:
                msg = resp.text
            raise CampaignMediaError(str(msg))
        return
    local_delete_poster_file(image_path)


async def delete_campaign_media_dir(campaign_id: int) -> None:
    if use_backend_media_api():
        url = f"{BACKEND_BASE_URL}/api/internal/campaigns/{int(campaign_id)}/media"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(url, headers=_headers())
        if resp.status_code >= 400 and resp.status_code != 404:
            try:
                body = resp.json()
                msg = body.get("message") or body.get("detail") or resp.text
            except Exception:
                msg = resp.text
            raise CampaignMediaError(str(msg))
        return
    local_delete_campaign_dir(campaign_id)


async def fetch_poster_bytes(image_path: str) -> tuple[bytes, str]:
    """返回 (content, content_type)。优先公开 /media（与桌面一致），再试内部接口，最后本地盘。"""
    parsed = parse_campaign_image_path(image_path)
    if not parsed:
        raise CampaignMediaError("无效的海报路径")
    cid, filename = parsed
    ctype_guess = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    base = public_media_base()
    if base:
        url = f"{base}/media/campaigns/{cid}/{filename}"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url)
        if resp.status_code == 200 and resp.content:
            ctype = resp.headers.get("content-type") or ctype_guess
            return resp.content, ctype

    if use_backend_media_api():
        url = f"{BACKEND_BASE_URL}/api/internal/media/campaigns/{cid}/{filename}"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url, headers=_headers())
        if resp.status_code >= 400:
            raise CampaignMediaError(f"获取海报失败 HTTP {resp.status_code}")
        ctype = resp.headers.get("content-type") or ctype_guess
        return resp.content, ctype

    local = _local_abs_path(image_path)
    if not local:
        raise CampaignMediaError("海报文件不存在")
    return local.read_bytes(), ctype_guess
