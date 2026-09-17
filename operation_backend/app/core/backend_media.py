"""通过核心 backend 内部 API 读写活动海报（跨容器部署）。

上传/删除：走 /api/internal/* + X-Internal-Key（BACKEND_BASE_URL，服务端可达即可）。
浏览器展示：默认同源相对路径 /media/...，由运营端代理到 backend（虚拟机友好）。
仅当显式配置 BACKEND_PUBLIC_URL 时，才让浏览器直连 backend。
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


def absolute_media_url(image_path: str | None) -> str | None:
    """给 <img> 用的地址：默认同源 /media/...；仅显式 BACKEND_PUBLIC_URL 时拼绝对地址。"""
    rel = (image_path or "").strip().replace("\\", "/")
    if not rel:
        return None
    if rel.startswith("http://") or rel.startswith("https://"):
        return rel
    if not rel.startswith("/"):
        rel = "/" + rel
    if BACKEND_PUBLIC_URL:
        return f"{BACKEND_PUBLIC_URL}{rel}"
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
    """服务端拉取原图：优先 BACKEND_BASE_URL（内网），再本地盘。"""
    parsed = parse_campaign_image_path(image_path)
    if not parsed:
        raise CampaignMediaError("无效的海报路径")
    cid, filename = parsed
    ctype_guess = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    if BACKEND_BASE_URL:
        url = f"{BACKEND_BASE_URL}/media/campaigns/{cid}/{filename}"
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


async def proxy_backend_media(rel_path: str) -> tuple[bytes, str, int]:
    """代理 GET /media/... → backend，返回 (body, content_type, status)。"""
    rel = (rel_path or "").strip().replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        raise CampaignMediaError("非法路径")
    if not BACKEND_BASE_URL:
        raise CampaignMediaError("未配置 BACKEND_BASE_URL")
    url = f"{BACKEND_BASE_URL}/media/{rel}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url)
    ctype = (
        resp.headers.get("content-type")
        or mimetypes.guess_type(rel)[0]
        or "application/octet-stream"
    )
    return resp.content, ctype, int(resp.status_code)
