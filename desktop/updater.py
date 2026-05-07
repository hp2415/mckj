import os
import sys
import json
import tempfile
import subprocess
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from PySide6.QtWidgets import QMessageBox

from logger_cfg import logger
from config_loader import cfg


@dataclass(frozen=True)
class LatestRelease:
    version: str
    download_url: str
    force: bool = True
    notes: str = ""


def _parse_semver(v: str) -> tuple[int, int, int, str]:
    """
    一个轻量级、无依赖的语义化版本（semver）解析器。
    支持格式：1.2.3, v1.2.3, 1.2.3-beta.1（预发布版本被视为较低版本）。
    """
    if not v:
        return (0, 0, 0, "invalid")
    raw = v.strip()
    if raw.startswith(("v", "V")):
        raw = raw[1:]
    main, _, pre = raw.partition("-")
    parts = main.split(".")
    try:
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
    except Exception:
        return (0, 0, 0, "invalid")
    return (major, minor, patch, pre or "")


def is_version_newer(latest: str, current: str) -> bool:
    lm, lmi, lp, lpre = _parse_semver(latest)
    cm, cmi, cp, cpre = _parse_semver(current)
    if (lm, lmi, lp) != (cm, cmi, cp):
        return (lm, lmi, lp) > (cm, cmi, cp)
    # 数字版本相同时：预发布版本（prerelease）被视为低于稳定版。
    if cpre and not lpre:
        return True
    return False


def get_current_version() -> str:
    try:
        from version import __version__  # type: ignore
        return str(__version__)
    except Exception:
        return "0.0.0"


async def fetch_latest_release(base_url: str) -> LatestRelease | None:
    url = f"{base_url.rstrip('/')}/api/system/desktop/latest"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning(f"更新检查失败: {resp.status_code} {resp.text[:200]}")
                return None
            data = resp.json()
    except Exception as e:
        logger.warning(f"更新检查异常: {e}")
        return None

    try:
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
            payload = data["data"]
        else:
            payload = data
        version = str(payload.get("version", "")).strip()
        download_url = str(payload.get("download_url", "")).strip()
        force = bool(payload.get("force", True))
        notes = str(payload.get("notes", "") or "")
        if not version or not download_url:
            return None
        # 支持相对路径 URL。
        if download_url.startswith("/"):
            download_url = urljoin(base_url.rstrip("/") + "/", download_url.lstrip("/"))
        return LatestRelease(version=version, download_url=download_url, force=force, notes=notes)
    except Exception:
        return None


async def download_to_temp(url: str, filename: str) -> str:
    tmp_dir = tempfile.gettempdir()
    dst = os.path.join(tmp_dir, filename)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=8.0)) as client:
            async with client.stream("GET", url, follow_redirects=True) as resp:
                resp.raise_for_status()
                with open(dst, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=1024 * 128):
                        f.write(chunk)
        return dst
    except Exception:
        # 确保删除下载了一半的文件。
        try:
            if os.path.exists(dst):
                os.remove(dst)
        except Exception:
            pass
        raise


def _launch_installer(installer_path: str) -> None:
    """
    启动安装程序并立即返回。
    在 Windows 上使用 os.startfile 以符合系统习惯。
    """
    if sys.platform.startswith("win"):
        os.startfile(installer_path)  # nosec - 正常启动本地安装包
        return
    # 备用方案（非 Windows 开发环境）
    subprocess.Popen([installer_path], close_fds=True)


def _hard_exit_for_update() -> None:
    """
    确保当前进程立即退出，以便安装程序可以替换可执行文件。
    对于强制更新流程，建议使用硬退出以避免文件锁竞争。
    """
    try:
        # 首先尝试尽力请求一个干净的 Qt 退出。
        try:
            from PySide6.QtWidgets import QApplication  # 延迟导入

            app = QApplication.instance()
            if app:
                app.quit()
        except Exception:
            pass

        if sys.platform.startswith("win"):
            os._exit(0)  # nosec - 更新程序需要的硬退出
        raise SystemExit(0)
    except Exception:
        # 最后手段：决不能让异常阻塞更新退出路径
        try:
            os._exit(0)  # nosec - 更新程序需要的硬退出
        except Exception:
            raise SystemExit(0)


async def enforce_latest_or_exit(parent_widget=None) -> bool:
    """
    如果应用可以继续运行则返回 True；如果已触发更新且应退出则返回 False。
    """
    base_url = cfg.api_url
    current = get_current_version()
    latest = await fetch_latest_release(base_url)
    if not latest:
        # 如果服务器未暴露更新信息，则允许应用继续（开发环境或离线状态）。
        return True

    if not is_version_newer(latest.version, current):
        return True

    title = "发现新版本，需要更新"
    msg = f"当前版本: {current}\n最新版本: {latest.version}\n\n将启动更新程序，完成更新后请重新打开客户端。"
    if latest.notes:
        msg += f"\n\n更新说明:\n{latest.notes}"

    QMessageBox.information(parent_widget, title, msg)

    try:
        fn = f"WeChatAI_Assistant_Setup_{latest.version}.exe"
        installer_path = await download_to_temp(latest.download_url, fn)
    except Exception as e:
        logger.error(f"下载更新包失败: {latest.download_url} | {e}")
        QMessageBox.critical(
            parent_widget,
            "更新失败",
            f"无法下载最新安装包。\n\n下载地址:\n{latest.download_url}\n\n错误信息:\n{e}",
        )
        # 强制更新场景：下载失败则阻止继续使用（防止旧版继续跑）
        return False

    try:
        _launch_installer(installer_path)
    except Exception as e:
        logger.error(f"启动安装包失败: {e}")
        QMessageBox.critical(
            parent_widget,
            "更新失败",
            "已下载更新包，但无法启动安装程序，请手动运行临时目录中的安装包。",
        )
        return False

    # 已触发更新。立即停止当前应用以释放文件锁。
    _hard_exit_for_update()
    return False  # pragma: no cover

