"""应用标识与旧版 WeChatAI_Assistant 迁移常量。"""
from __future__ import annotations

import os
import shutil
import sys

APP_NAME = "Mibuddy_Assistant"
APP_EXE_NAME = "Mibuddy_Assistant.exe"
UPDATER_EXE_NAME = "Mibuddy_Updater.exe"
SETUP_EXE_NAME = "Mibuddy_Assistant_Setup.exe"
DISPLAY_NAME = "米宝(Mibuddy)"
APP_MUTEX_NAME = "Mibuddy.AppMutex"
WINDOW_TITLE_MARKER = "米宝"

LEGACY_APP_NAME = "WeChatAI_Assistant"
LEGACY_APP_EXE_NAME = "WeChatAI_Assistant.exe"
LEGACY_UPDATER_EXE_NAME = "WeChatAI_Updater.exe"
LEGACY_SETUP_EXE_NAME = "WeChatAI_Assistant_Setup.exe"
LEGACY_APP_MUTEX_NAME = "WeChatAI.Assistant.AppMutex"
LEGACY_WINDOW_TITLE_MARKERS = ("微企 AI",)

LEGACY_INSTALL_FILES = (
    LEGACY_APP_EXE_NAME,
    LEGACY_UPDATER_EXE_NAME,
)


def local_appdata_root() -> str:
    return os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")


def app_data_dir_for_exe() -> str:
    if getattr(sys, "frozen", False):
        name = os.path.splitext(os.path.basename(sys.executable))[0] or APP_NAME
        return os.path.join(local_appdata_root(), name)
    return os.path.join(local_appdata_root(), APP_NAME)


def canonical_app_data_dir() -> str:
    return os.path.join(local_appdata_root(), APP_NAME)


def legacy_app_data_dir() -> str:
    return os.path.join(local_appdata_root(), LEGACY_APP_NAME)


def process_image_names() -> tuple[str, ...]:
    return (APP_EXE_NAME, LEGACY_APP_EXE_NAME)


def setup_image_names() -> tuple[str, ...]:
    return (SETUP_EXE_NAME, LEGACY_SETUP_EXE_NAME)


def updater_exe_names() -> tuple[str, ...]:
    return (UPDATER_EXE_NAME, LEGACY_UPDATER_EXE_NAME)


def migrate_legacy_user_data() -> None:
    """将 %LOCALAPPDATA%\\WeChatAI_Assistant 合并到 Mibuddy_Assistant（若尚未迁移）。"""
    src = legacy_app_data_dir()
    dst = canonical_app_data_dir()
    if not os.path.isdir(src) or os.path.normcase(src) == os.path.normcase(dst):
        return

    os.makedirs(dst, exist_ok=True)
    for name in ("config.ini", "pending_update.json", "update_in_progress.json"):
        source = os.path.join(src, name)
        target = os.path.join(dst, name)
        if os.path.isfile(source) and not os.path.isfile(target):
            try:
                shutil.copy2(source, target)
            except OSError:
                pass

    src_updates = os.path.join(src, "updates")
    dst_updates = os.path.join(dst, "updates")
    if not os.path.isdir(src_updates):
        return

    os.makedirs(dst_updates, exist_ok=True)
    for entry in os.listdir(src_updates):
        source = os.path.join(src_updates, entry)
        target = os.path.join(dst_updates, entry)
        if os.path.exists(target):
            continue
        try:
            if os.path.isdir(source):
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        except OSError:
            pass


def cleanup_legacy_install_files(install_dir: str) -> None:
    if not install_dir:
        return
    for name in LEGACY_INSTALL_FILES:
        path = os.path.join(install_dir, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
