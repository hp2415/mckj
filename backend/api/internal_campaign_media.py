"""运营后台内调：活动海报文件读写（落盘 backend/media，与桌面 /media 一致）。"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from core.campaign_media import (
    CampaignMediaError,
    campaign_dir,
    delete_campaign_dir,
    delete_poster_file,
    media_root,
    public_media_path,
    save_campaign_poster,
)
from core.upload_limits import UploadLimitError, read_capped_upload

router = APIRouter(prefix="/api/internal", tags=["internal-campaign-media"])

_MAX = 8 * 1024 * 1024


def _require_internal_key(
    x_internal_key: str | None = Header(default=None, alias="X-Internal-Key"),
) -> None:
    expected = (os.getenv("INTERNAL_API_KEY") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="INTERNAL_API_KEY 未配置")
    if not x_internal_key or x_internal_key.strip() != expected:
        raise HTTPException(status_code=401, detail="内部鉴权失败")


def _safe_campaign_file(campaign_id: int, filename: str) -> Path:
    name = Path(filename or "").name
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="非法文件名")
    root = (media_root() / "campaigns" / str(int(campaign_id))).resolve()
    target = (root / name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法路径") from exc
    return target


@router.post("/campaigns/{campaign_id}/posters")
async def internal_upload_posters(
    campaign_id: int,
    files: list[UploadFile] = File(...),
    _: None = Depends(_require_internal_key),
):
    """仅写文件并返回 image_path；库表由运营后台维护。"""
    if campaign_id <= 0:
        raise HTTPException(status_code=400, detail="无效活动 ID")
    saved: list[dict] = []
    try:
        for upload in files or []:
            filename = str(getattr(upload, "filename", "") or "")
            if not filename:
                continue
            content = await read_capped_upload(upload, max_bytes=_MAX)
            rel = save_campaign_poster(int(campaign_id), filename, content)
            saved.append({"image_path": rel, "filename": Path(rel).name})
    except UploadLimitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CampaignMediaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not saved:
        raise HTTPException(status_code=400, detail="请选择至少一张图片")
    return {"code": 200, "message": f"已上传 {len(saved)} 张", "data": {"items": saved}}


@router.get("/media/campaigns/{campaign_id}/{filename}")
async def internal_get_poster(
    campaign_id: int,
    filename: str,
    _: None = Depends(_require_internal_key),
):
    """内网拉取原图（运营后台代理可用）；浏览器公开访问仍走 /media 静态挂载。"""
    path = _safe_campaign_file(campaign_id, filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path)


@router.delete("/media/campaigns/{campaign_id}/{filename}")
async def internal_delete_poster(
    campaign_id: int,
    filename: str,
    _: None = Depends(_require_internal_key),
):
    rel = public_media_path(int(campaign_id), Path(filename).name)
    delete_poster_file(rel)
    return {"code": 200, "message": "ok", "data": None}


@router.delete("/campaigns/{campaign_id}/media")
async def internal_delete_campaign_media(
    campaign_id: int,
    _: None = Depends(_require_internal_key),
):
    delete_campaign_dir(int(campaign_id))
    # 确保目录存在性不影响结果
    dest = campaign_dir(int(campaign_id))
    return {
        "code": 200,
        "message": "ok",
        "data": {"removed": not dest.exists()},
    }
