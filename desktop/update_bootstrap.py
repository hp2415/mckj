"""
独立更新引导器（打包为 WeChatAI_Updater.exe）。

由主程序在下载并校验安装包后拉起；等待主进程退出后启动安装向导（传统界面），避免文件锁竞争。
仅使用标准库且不导入 ctypes/tkinter，降低 PyInstaller 对 ffi/tcl 等 DLL 的依赖。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

_APP_NAME = "WeChatAI_Assistant"
_APP_EXE_NAME = "WeChatAI_Assistant.exe"
# 传统安装向导（不用 /VERYSILENT）；保留防重启与自动关旧进程
_INSTALLER_ARGS = (
    "/NORESTART",
    "/NORESTARTAPPLICATIONS",
    "/CLOSEAPPLICATIONS",
    "/FORCECLOSEAPPLICATIONS",
)
_WAIT_PID_TIMEOUT_SEC = 90
_WAIT_PROCESS_STOP_SEC = 45
_POST_EXIT_SETTLE_SEC = 2.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROCESS_IMAGE_NAMES = (_APP_EXE_NAME,)


def _app_data_dir() -> str:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(root, _APP_NAME)


def _update_lock_path() -> str:
    return os.path.join(_app_data_dir(), "update_in_progress.json")


def _log_path() -> str:
    return os.path.join(_app_data_dir(), "updater.log")


def _install_log_path() -> str:
    return os.path.join(_app_data_dir(), "install.log")


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        os.makedirs(_app_data_dir(), exist_ok=True)
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _clear_update_lock() -> None:
    try:
        path = _update_lock_path()
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _read_pending(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("pending_update.json 格式无效")
    return data


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=_NO_WINDOW,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
        if "no tasks" in text.lower():
            return False
        return str(pid) in text
    except Exception:
        return False


def _wait_for_pid(pid: int, timeout_sec: float) -> bool:
    if pid <= 0:
        return True
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not _is_pid_running(pid):
            return True
        time.sleep(1.0)
    return not _is_pid_running(pid)


def _is_process_image_running(image_name: str) -> bool:
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=_NO_WINDOW,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if "no tasks" in out.lower():
            return False
        return image_name.lower() in out.lower()
    except Exception:
        return False


def _wait_for_processes_stopped(timeout_sec: float) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not any(_is_process_image_running(name) for name in _PROCESS_IMAGE_NAMES):
            return
        time.sleep(1.0)


def _append_install_log_tail() -> None:
    path = _install_log_path()
    try:
        if not os.path.isfile(path):
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            tail = f.read()[-12000:]
        _log("--- Inno Setup install.log (tail) ---")
        for line in tail.splitlines():
            _log(line)
    except Exception as e:
        _log(f"读取 install.log 失败: {e}")


def _run_installer_subprocess(installer_path: str, *, elevated: bool) -> int:
    log_path = _install_log_path()
    try:
        if os.path.exists(log_path):
            os.remove(log_path)
    except Exception:
        pass

    cli_args = [* _INSTALLER_ARGS, f"/LOG={log_path}"]
    mode = "提权" if elevated else "普通"
    _log(f"启动安装包({mode}): {' '.join(cli_args)}")

    if not elevated:
        cmd = [installer_path, *cli_args]
        proc = subprocess.run(cmd, timeout=1800)
        return int(proc.returncode or 0)

    # D:\ 等目录的覆盖安装通常需要 UAC
    arg_ps = ",".join("'" + a.replace("'", "''") + "'" for a in cli_args)
    installer_ps = installer_path.replace("'", "''")
    ps = (
        f"$p = Start-Process -FilePath '{installer_ps}' "
        f"-ArgumentList @({arg_ps}) -Verb RunAs -Wait -PassThru; "
        "if ($null -eq $p) { exit 1 }; exit $p.ExitCode"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        creationflags=_NO_WINDOW,
        timeout=1800,
    )
    return int(proc.returncode or 0)


def _run_installer(installer_path: str) -> int:
    if not os.path.isfile(installer_path):
        raise FileNotFoundError(f"安装包不存在: {installer_path}")

    code = _run_installer_subprocess(installer_path, elevated=False)
    if code == 0:
        return 0

    _log(f"普通权限安装失败 exit={code}，尝试 UAC 提权重试")
    code = _run_installer_subprocess(installer_path, elevated=True)
    if code != 0:
        _append_install_log_tail()
    return code


def _launch_app_fallback(app_exe_path: str) -> None:
    if not app_exe_path or not os.path.isfile(app_exe_path):
        return
    try:
        subprocess.Popen(
            [app_exe_path],
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        _log(f"兜底拉起客户端: {app_exe_path}")
    except Exception as e:
        _log(f"兜底拉起客户端失败: {e}")


def _show_error(message: str) -> None:
    _log(message)
    try:
        safe = message.replace("'", "''").replace("\r", "").replace("\n", "`n")
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            f"[System.Windows.Forms.MessageBox]::Show('{safe}', '更新失败', 'OK', 'Error')"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=_NO_WINDOW,
            timeout=30,
        )
    except Exception:
        pass


def run_update(pending_path: str) -> int:
    try:
        pending = _read_pending(pending_path)
        installer_path = str(pending.get("installer_path") or "").strip()
        main_pid = int(pending.get("main_pid") or 0)
        app_exe_path = str(pending.get("app_exe_path") or "").strip()
        target_version = str(pending.get("target_version") or "").strip()

        _log(f"开始更新引导 target={target_version} main_pid={main_pid}")

        if not installer_path:
            raise ValueError("pending_update.json 缺少 installer_path")

        _log(f"等待主进程退出 pid={main_pid}")
        exited = _wait_for_pid(main_pid, _WAIT_PID_TIMEOUT_SEC)
        if not exited:
            _log("等待主进程 PID 超时，将继续等待进程名退出")

        _wait_for_processes_stopped(_WAIT_PROCESS_STOP_SEC)
        if any(_is_process_image_running(name) for name in _PROCESS_IMAGE_NAMES):
            _log(f"仍有 {', '.join(_PROCESS_IMAGE_NAMES)} 在运行，安装可能失败")
        else:
            _log("目标进程已全部退出")

        time.sleep(_POST_EXIT_SETTLE_SEC)

        code = _run_installer(installer_path)
        _log(f"安装包退出码: {code}")

        if code != 0:
            hint = (
                f"安装未能完成（退出码 {code}）。\n\n"
                "常见原因：安装目录无写入权限、程序文件仍被占用。\n"
                f"详细日志：{_install_log_path()}\n\n"
                "可尝试手动运行安装包完成更新。"
            )
            _show_error(hint)
            _clear_update_lock()
            return code

        _clear_update_lock()

        # 安装向导结束页通常已提供「立即运行」，此处仅作兜底
        time.sleep(2.0)
        if app_exe_path and not _is_process_image_running(os.path.basename(app_exe_path)):
            _launch_app_fallback(app_exe_path)

        _log("更新引导完成")
        return 0
    except Exception as e:
        _log(f"更新引导异常: {e}")
        _clear_update_lock()
        _show_error(f"更新过程出现异常：\n{e}")
        return 1


def default_pending_path() -> str:
    return os.path.join(_app_data_dir(), "pending_update.json")


def main() -> None:
    pending_path = sys.argv[1].strip() if len(sys.argv) > 1 else default_pending_path()
    code = run_update(pending_path)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
