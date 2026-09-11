"""活动海报文件：写入 backend/media/campaigns/{id}/，经 /media 提供。"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_JPEG = b"\xff\xd8\xff"
_PNG = b"\x89PNG\r\n\x1a\n"
_MAX_POSTER_BYTES = 8 * 1024 * 1024
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class CampaignMediaError(ValueError):
    pass


def media_root() -> Path:
    raw = (os.getenv("MEDIA_DIR") or "").strip()
    return Path(raw) if raw else (_BACKEND_DIR / "media")


def campaign_dir(campaign_id: int) -> Path:
    return media_root() / "campaigns" / str(int(campaign_id))


def public_media_path(campaign_id: int, filename: str) -> str:
    return f"/media/campaigns/{int(campaign_id)}/{filename}"


def detect_image_ext(filename: str, content: bytes) -> str:
    name = (filename or "").strip().lower()
    ext = Path(name).suffix
    if content.startswith(_JPEG):
        return ".jpg"
    if content.startswith(_PNG):
        return ".png"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    if ext in _ALLOWED_EXT:
        if ext == ".jpeg":
            return ".jpg"
        return ext
    raise CampaignMediaError("仅支持 JPG / PNG / WEBP 图片")


def save_campaign_poster(campaign_id: int, filename: str, content: bytes) -> str:
    if not content:
        raise CampaignMediaError("上传文件为空")
    if len(content) > _MAX_POSTER_BYTES:
        raise CampaignMediaError("图片过大，最大允许 8MB")
    ext = detect_image_ext(filename, content)
    stem = _UNSAFE_NAME.sub("_", Path(filename or "poster").stem)[:40] or "poster"
    stored = f"{uuid.uuid4().hex[:12]}_{stem}{ext}"
    dest_dir = campaign_dir(campaign_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / stored
    dest.write_bytes(content)
    return public_media_path(campaign_id, stored)


def delete_poster_file(image_path: str) -> None:
    rel = (image_path or "").strip().replace("\\", "/")
    if not rel.startswith("/media/campaigns/"):
        return
    parts = rel.split("/")
    # /media/campaigns/{id}/{file}
    if len(parts) < 5:
        return
    abs_path = media_root() / "campaigns" / parts[3] / parts[4]
    try:
        resolved = abs_path.resolve()
        resolved.relative_to((media_root() / "campaigns").resolve())
        if resolved.is_file():
            resolved.unlink()
    except (OSError, ValueError):
        pass


def delete_campaign_dir(campaign_id: int) -> None:
    dest = campaign_dir(campaign_id)
    if not dest.is_dir():
        return
    try:
        for child in dest.iterdir():
            if child.is_file():
                child.unlink()
        dest.rmdir()
    except OSError:
        pass


def blast_custom_dir(job_id: int) -> Path:
    return media_root() / "blast_custom" / str(int(job_id))


def public_blast_custom_path(job_id: int, filename: str) -> str:
    return f"/media/blast_custom/{int(job_id)}/{filename}"


def save_blast_custom_image(job_id: int, filename: str, content: bytes) -> str:
    if not content:
        raise CampaignMediaError("上传文件为空")
    if len(content) > _MAX_POSTER_BYTES:
        raise CampaignMediaError("图片过大，最大允许 8MB")
    ext = detect_image_ext(filename, content)
    stem = _UNSAFE_NAME.sub("_", Path(filename or "custom").stem)[:40] or "custom"
    stored = f"{uuid.uuid4().hex[:12]}_{stem}{ext}"
    dest_dir = blast_custom_dir(job_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / stored
    dest.write_bytes(content)
    return public_blast_custom_path(job_id, stored)


def delete_blast_custom_file(image_path: str) -> None:
    rel = (image_path or "").strip().replace("\\", "/")
    if not rel.startswith("/media/blast_custom/"):
        return
    parts = rel.split("/")
    # /media/blast_custom/{job_id}/{file}
    if len(parts) < 5:
        return
    abs_path = media_root() / "blast_custom" / parts[3] / parts[4]
    try:
        resolved = abs_path.resolve()
        resolved.relative_to((media_root() / "blast_custom").resolve())
        if resolved.is_file():
            resolved.unlink()
    except (OSError, ValueError):
        pass
