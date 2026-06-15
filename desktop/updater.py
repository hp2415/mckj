import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMessageBox, QProgressDialog

from logger_cfg import logger
from config_loader import CANONICAL_API_URL, LEGACY_API_URLS, cfg, normalize_api_url

_UPDATER_EXE_NAME = "WeChatAI_Updater.exe"
_SETUP_IMAGE_NAMES = (
    "WeChatAI_Assistant_Setup.exe",
)
_UPDATE_LOCK_TTL_SEC = 30 * 60
_STALE_LOCK_WITHOUT_PROCESS_SEC = 15
_DOWNLOAD_RETRIES = 4
_DOWNLOAD_STALL_SEC = 600
_DOWNLOAD_CHUNK = 1024 * 128

# 主进程已退出后由引导器启动安装向导
_INSTALLER_ARGS = (
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/NORESTARTAPPLICATIONS",
)


@dataclass(frozen=True)
class LatestRelease:
    version: str
    download_url: str
    force: bool = True
    notes: str = ""
    sha256: str = ""
    size: int = 0


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
    if cpre and not lpre:
        return True
    return False


def get_current_version() -> str:
    try:
        from version import __version__  # type: ignore

        return str(__version__)
    except Exception:
        return "0.0.0"


def _app_data_dir() -> str:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or tempfile.gettempdir()
    return os.path.join(root, "WeChatAI_Assistant")


def _updates_dir() -> str:
    return os.path.join(_app_data_dir(), "updates")


def _update_lock_path() -> str:
    return os.path.join(_app_data_dir(), "update_in_progress.json")


def _pending_update_path() -> str:
    return os.path.join(_app_data_dir(), "pending_update.json")


def _installer_cache_path(version: str) -> str:
    safe = version.replace("/", "_").replace("\\", "_")
    return os.path.join(_updates_dir(), f"WeChatAI_Assistant_Setup_{safe}.exe")


async def probe_api_base(base_url: str) -> bool:
    """探测后端是否可达（不要求已配置桌面端更新信息）。"""
    url = f"{base_url.rstrip('/')}/api/system/desktop/latest"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            return resp.status_code == 200
    except Exception:
        return False


async def resolve_update_base_url() -> str:
    """
    选择用于更新检查的 API 基础地址。
    优先当前配置；若不可达则尝试权威新地址（便于旧客户端在服务器迁移后仍能拉到更新包）。
    api_url_lock=true 时仅使用 config.ini 中的地址，不回落到生产环境（便于开发环境测试更新）。
    """
    current = cfg.api_url
    candidates: list[str] = [current]
    if not cfg.api_url_locked:
        normalized_canonical = normalize_api_url(CANONICAL_API_URL)
        if normalized_canonical and normalized_canonical not in candidates:
            candidates.append(normalized_canonical)

    for url in candidates:
        if await probe_api_base(url):
            if normalize_api_url(url) != normalize_api_url(current):
                logger.info(f"更新检查：当前地址不可达，改用 {url}")
            if (
                not cfg.api_url_locked
                and normalize_api_url(current) in LEGACY_API_URLS
                and normalize_api_url(current) != CANONICAL_API_URL
            ):
                cfg.set_api_url(CANONICAL_API_URL)
                logger.info(f"已将 API 地址迁移至 {CANONICAL_API_URL}")
            return url

    if cfg.api_url_locked:
        logger.warning(f"更新检查：已锁定 api_url={current}，且该地址不可达，跳过回落到生产环境")
    return current


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
        sha256 = str(payload.get("sha256", "") or "").strip().lower()
        size_raw = payload.get("size", 0)
        try:
            size = int(size_raw) if size_raw else 0
        except Exception:
            size = 0
        if not version or not download_url:
            return None
        if download_url.startswith("/"):
            download_url = urljoin(base_url.rstrip("/") + "/", download_url.lstrip("/"))
        return LatestRelease(
            version=version,
            download_url=download_url,
            force=force,
            notes=notes,
            sha256=sha256,
            size=size,
        )
    except Exception:
        return None


def _pump_qt_events() -> None:
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app:
            app.processEvents()
    except Exception:
        pass


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_file_quiet(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _validate_installer_file(path: str, *, expected_sha256: str, expected_size: int) -> bool:
    if not os.path.isfile(path):
        return False
    actual_size = os.path.getsize(path)
    if expected_size > 0 and actual_size != expected_size:
        logger.warning(f"安装包大小不符: expected={expected_size} actual={actual_size}")
        return False
    if expected_sha256:
        actual_sha = _sha256_file(path)
        if actual_sha.lower() != expected_sha256.lower():
            logger.warning(f"安装包 sha256 不符: expected={expected_sha256} actual={actual_sha}")
            return False
    return True


async def download_installer(
    url: str,
    dst: str,
    *,
    expected_sha256: str = "",
    expected_size: int = 0,
    on_progress=None,
    cancel_event: asyncio.Event | None = None,
    retries: int = _DOWNLOAD_RETRIES,
) -> str:
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    if os.path.isfile(dst) and _validate_installer_file(
        dst, expected_sha256=expected_sha256, expected_size=expected_size
    ):
        if on_progress:
            on_progress(100, expected_size or os.path.getsize(dst))
        return dst

    last_error: Exception | None = None
    for attempt in range(retries):
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError("用户取消下载")

        if attempt > 0:
            delay = min(2 ** attempt, 16)
            logger.info(f"下载重试 {attempt + 1}/{retries}，{delay}s 后重试")
            await asyncio.sleep(delay)
            _remove_file_quiet(dst)

        existing = os.path.getsize(dst) if os.path.isfile(dst) else 0
        headers: dict[str, str] = {}
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"

        try:
            timeout = httpx.Timeout(60.0, connect=8.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("GET", url, headers=headers, follow_redirects=True) as resp:
                    if resp.status_code == 416:
                        _remove_file_quiet(dst)
                        existing = 0
                        headers.pop("Range", None)
                        async with client.stream("GET", url, follow_redirects=True) as resp2:
                            resp = resp2
                    elif resp.status_code not in (200, 206):
                        resp.raise_for_status()

                    total = expected_size
                    if total <= 0:
                        content_length = resp.headers.get("content-length")
                        if content_length and resp.status_code == 200:
                            total = int(content_length)
                        elif content_length and resp.status_code == 206:
                            total = existing + int(content_length)

                    mode = "ab" if existing > 0 and resp.status_code == 206 else "wb"
                    if mode == "wb" and existing > 0:
                        _remove_file_quiet(dst)
                        existing = 0

                    downloaded = existing
                    last_progress_at = time.time()
                    last_bytes = downloaded
                    last_ui_at = 0.0

                    with open(dst, mode) as f:
                        async for chunk in resp.aiter_bytes(chunk_size=_DOWNLOAD_CHUNK):
                            if cancel_event and cancel_event.is_set():
                                raise asyncio.CancelledError("用户取消下载")
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()
                            if downloaded != last_bytes:
                                last_bytes = downloaded
                                last_progress_at = now
                            elif now - last_progress_at > _DOWNLOAD_STALL_SEC:
                                raise TimeoutError("下载长时间无进展")
                            if now - last_ui_at >= 0.2:
                                last_ui_at = now
                                if on_progress:
                                    pct = int(downloaded * 100 / total) if total > 0 else 0
                                    on_progress(min(pct, 99), downloaded)
                                _pump_qt_events()

            if not _validate_installer_file(
                dst, expected_sha256=expected_sha256, expected_size=expected_size
            ):
                _remove_file_quiet(dst)
                raise ValueError("安装包校验失败")

            if on_progress:
                on_progress(100, os.path.getsize(dst))
            return dst
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_error = e
            logger.warning(f"下载失败 attempt={attempt + 1}: {e}")

    _remove_file_quiet(dst)
    if last_error:
        raise last_error
    raise RuntimeError("下载失败")


def _read_update_lock() -> dict | None:
    path = _update_lock_path()
    try:
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_update_lock(*, target_version: str) -> None:
    path = _update_lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "target_version": target_version,
        "started_at": time.time(),
        "installer": os.path.basename(sys.executable) if getattr(sys, "frozen", False) else "dev",
        "main_pid": os.getpid(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def clear_update_lock() -> None:
    try:
        path = _update_lock_path()
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _is_process_image_running(image_name: str) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return image_name.lower() in out.lower()
    except Exception:
        return False


def _is_updater_running() -> bool:
    return _is_process_image_running(_UPDATER_EXE_NAME)


def _is_setup_installer_running() -> bool:
    return any(_is_process_image_running(name) for name in _SETUP_IMAGE_NAMES)


def _is_update_in_progress() -> bool:
    if _is_updater_running() or _is_setup_installer_running():
        return True

    data = _read_update_lock()
    if not data:
        return False

    started = float(data.get("started_at") or 0)
    if started <= 0:
        clear_update_lock()
        return False

    age = time.time() - started
    if age >= _UPDATE_LOCK_TTL_SEC:
        clear_update_lock()
        return False

    if age >= _STALE_LOCK_WITHOUT_PROCESS_SEC:
        # 引导器秒退（如 DLL 缺失）时，无进程但锁仍在；短宽限期后自动清锁
        clear_update_lock()
        return False

    # 锁很新且尚无可见进程：可能刚 spawn，短暂等待
    return True


def _write_pending_update(
    *,
    installer_path: str,
    target_version: str,
    expected_sha256: str,
) -> str:
    path = _pending_update_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    app_exe = sys.executable if getattr(sys, "frozen", False) else ""
    payload = {
        "installer_path": installer_path,
        "target_version": target_version,
        "main_pid": os.getpid(),
        "expected_sha256": expected_sha256,
        "app_exe_path": app_exe,
        "created_at": time.time(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


def _resolve_updater_exe() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        app_dir = os.path.dirname(sys.executable)
        bundled = os.path.join(app_dir, _UPDATER_EXE_NAME)
        if os.path.isfile(bundled):
            return bundled
    for candidate in (
        os.path.join(here, "dist", _UPDATER_EXE_NAME),
        os.path.join(here, "update_bootstrap.py"),
    ):
        if os.path.isfile(candidate):
            return candidate
    raise FileNotFoundError(f"找不到更新引导程序 {_UPDATER_EXE_NAME}")


def spawn_detached_updater(pending_path: str) -> None:
    updater = _resolve_updater_exe()
    if updater.lower().endswith(".py"):
        cmd = [sys.executable, updater, pending_path]
        cwd = os.path.dirname(updater)
    else:
        cmd = [updater, pending_path]
        cwd = os.path.dirname(updater)

    logger.info(f"启动更新引导器: {' '.join(cmd)}")
    if sys.platform.startswith("win"):
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        subprocess.Popen(  # nosec - 启动本地更新引导器
            cmd,
            creationflags=detached | new_group,
            close_fds=True,
            cwd=cwd,
        )
        return
    subprocess.Popen(cmd, close_fds=True, cwd=cwd)  # nosec


def _show_update_busy_message(parent_widget, *, reason: str) -> None:
    QMessageBox.warning(
        parent_widget,
        "正在更新，请稍候",
        f"{reason}\n\n"
        "更新正在后台进行，请耐心等待。\n"
        "不要重复打开本客户端。\n"
        "完成后会自动打开新版本；若长时间无响应，请从开始菜单重新打开。",
    )


def _show_download_failure_dialog(parent_widget, *, download_url: str, error: str) -> str:
    """
    返回: retry | browser | copy | exit
    """
    box = QMessageBox(parent_widget)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle("更新失败")
    box.setText(
        "无法下载最新安装包。\n\n"
        f"错误信息：{error}\n\n"
        "您可以重试下载，或用浏览器手动下载安装包。"
    )
    retry_btn = box.addButton("重试", QMessageBox.ButtonRole.AcceptRole)
    browser_btn = box.addButton("用浏览器打开", QMessageBox.ButtonRole.ActionRole)
    copy_btn = box.addButton("复制下载链接", QMessageBox.ButtonRole.ActionRole)
    exit_btn = box.addButton("退出", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(retry_btn)
    box.exec()

    clicked = box.clickedButton()
    if clicked is browser_btn:
        try:
            webbrowser.open(download_url)
        except Exception as e:
            logger.warning(f"打开浏览器失败: {e}")
        return "browser"
    if clicked is copy_btn:
        try:
            QGuiApplication.clipboard().setText(download_url)
            QMessageBox.information(parent_widget, "已复制", "下载链接已复制到剪贴板。")
        except Exception as e:
            logger.warning(f"复制链接失败: {e}")
        return "copy"
    if clicked is exit_btn:
        return "exit"
    return "retry"


async def _download_with_progress(
    parent_widget,
    latest: LatestRelease,
) -> str | None:
    cancel_event = asyncio.Event()
    dst = _installer_cache_path(latest.version)

    progress = QProgressDialog("正在下载更新包…", "取消", 0, 100, parent_widget)
    progress.setWindowTitle("正在更新")
    progress.setWindowModality(Qt.WindowModality.ApplicationModal)
    progress.setMinimumDuration(0)
    progress.setValue(0)
    progress.show()

    def on_progress(pct: int, _downloaded: int) -> None:
        progress.setValue(max(0, min(100, pct)))
        progress.setLabelText(f"正在下载更新包… {pct}%")
        _pump_qt_events()

    def on_cancel() -> None:
        cancel_event.set()

    progress.canceled.connect(on_cancel)

    try:
        return await download_installer(
            latest.download_url,
            dst,
            expected_sha256=latest.sha256,
            expected_size=latest.size,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
    except asyncio.CancelledError:
        logger.info("用户取消下载")
        return None
    finally:
        progress.close()


async def enforce_latest_or_exit(parent_widget=None) -> bool:
    """
    如果应用可以继续运行则返回 True；如果已触发更新且应退出则返回 False。
    """
    base_url = await resolve_update_base_url()
    current = get_current_version()
    latest = await fetch_latest_release(base_url)
    logger.info(
        f"更新检查: api={base_url} 当前版本={current} "
        f"服务端最新={latest.version if latest else '(未获取)'}"
    )
    if not latest:
        return True

    if not is_version_newer(latest.version, current):
        clear_update_lock()
        return True

    if _is_update_in_progress():
        _show_update_busy_message(
            parent_widget,
            reason="检测到更新程序或安装程序可能已在运行。",
        )
        return False

    title = "发现新版本，需要更新"
    msg = (
        f"当前版本: {current}\n最新版本: {latest.version}\n\n"
        "点击「确定」后将自动下载更新包并打开安装向导。\n"
        "安装过程中请勿重复打开本客户端。\n"
        "请在安装窗口中按提示完成安装，完成后可重新打开客户端。"
    )
    if latest.notes:
        msg += f"\n\n更新说明:\n{latest.notes}"

    box = QMessageBox(parent_widget)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle(title)
    box.setText(msg)
    box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(QMessageBox.StandardButton.Ok)
    if box.exec() != QMessageBox.StandardButton.Ok:
        return False

    _write_update_lock(target_version=latest.version)

    installer_path: str | None = None
    while installer_path is None:
        try:
            installer_path = await _download_with_progress(parent_widget, latest)
            if installer_path is None:
                action = _show_download_failure_dialog(
                    parent_widget,
                    download_url=latest.download_url,
                    error="下载已取消",
                )
                if action == "retry":
                    continue
                clear_update_lock()
                return False
        except Exception as e:
            logger.error(f"下载更新包失败: {latest.download_url} | {e}")
            action = _show_download_failure_dialog(
                parent_widget,
                download_url=latest.download_url,
                error=str(e),
            )
            if action == "retry":
                continue
            clear_update_lock()
            return False

    if not cfg.api_url_locked and normalize_api_url(cfg.api_url) in LEGACY_API_URLS:
        cfg.set_api_url(CANONICAL_API_URL)

    try:
        pending_path = _write_pending_update(
            installer_path=installer_path,
            target_version=latest.version,
            expected_sha256=latest.sha256,
        )
        spawn_detached_updater(pending_path)
    except Exception as e:
        logger.error(f"启动更新引导器失败: {e}")
        clear_update_lock()
        QMessageBox.critical(
            parent_widget,
            "更新失败",
            f"已下载更新包，但无法启动更新程序。\n\n错误：{e}\n\n"
            f"请手动运行安装包：\n{installer_path}",
        )
        return False

    QMessageBox.information(
        parent_widget,
        "正在更新",
        "更新包已就绪，本客户端即将关闭。\n\n"
        "随后将弹出安装向导，请按提示完成安装。\n"
        "若出现权限提示，请点击「是」。\n"
        "请勿重复打开本客户端。",
    )

    # 返回 False 让 main.py 走优雅退出，引导器会在主进程退出后再安装
    return False
