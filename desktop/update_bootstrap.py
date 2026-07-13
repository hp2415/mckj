"""
独立更新引导器（打包为 Mibuddy_Updater.exe，兼容旧版 WeChatAI_Updater.exe）。

由主程序在下载并校验安装包后拉起；等待主进程退出后静默安装，避免文件锁竞争。
仅使用标准库且不导入 ctypes/tkinter，降低 PyInstaller 对 ffi/tcl 等 DLL 的依赖。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from app_identity import (
    APP_EXE_NAME,
    APP_NAME,
    DISPLAY_NAME,
    cleanup_legacy_install_files,
    legacy_app_data_dir,
    migrate_legacy_user_data,
    process_image_names,
)

_APP_NAME = APP_NAME
_APP_EXE_NAME = APP_EXE_NAME
_DISPLAY_NAME = DISPLAY_NAME
# 自动更新必须静默且关闭 Restart Manager：
# 1) /CLOSEAPPLICATIONS 会在部分机器上卡死任务栏
# 2) 交互向导取消后若再提权重试，会再弹安装窗并触发安全软件拦截 PowerShell RunAs
_INSTALLER_ARGS = (
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/NORESTARTAPPLICATIONS",
    "/NOCLOSEAPPLICATIONS",
    "/SP-",
)
# Inno：2=向导取消，5=安装中取消/Abort
_INNO_EXIT_USER_CANCEL = frozenset({2, 5})
_WAIT_PID_TIMEOUT_SEC = 90
_WAIT_PROCESS_STOP_SEC = 45
_POST_EXIT_SETTLE_SEC = 2.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROCESS_IMAGE_NAMES = process_image_names()


def _app_data_dir() -> str:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    for candidate in (
        os.path.join(root, _APP_NAME),
        legacy_app_data_dir(),
    ):
        pending = os.path.join(candidate, "pending_update.json")
        if os.path.isfile(pending):
            return candidate
    return os.path.join(root, _APP_NAME)


def _update_lock_path() -> str:
    return os.path.join(_app_data_dir(), "update_in_progress.json")


def _log_path() -> str:
    return os.path.join(_app_data_dir(), "updater.log")


def _install_log_path() -> str:
    return os.path.join(_app_data_dir(), "install.log")


def _progress_status_path() -> str:
    return os.path.join(_app_data_dir(), "update_progress_status.txt")


def _progress_script_path() -> str:
    return os.path.join(_app_data_dir(), "update_progress.ps1")


def _progress_meta_path() -> str:
    return os.path.join(_app_data_dir(), "update_progress_meta.txt")


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


def _set_progress(percent: int, text: str) -> None:
    """状态文件用 UTF-16，避免中文 Windows 下 PowerShell 读 UTF-8 乱码。格式: 百分比|文案"""
    try:
        os.makedirs(_app_data_dir(), exist_ok=True)
        pct = max(0, min(100, int(percent)))
        with open(_progress_status_path(), "w", encoding="utf-16") as f:
            f.write(f"{pct}|{text}")
    except Exception:
        pass


def _set_progress_done() -> None:
    try:
        with open(_progress_status_path(), "w", encoding="utf-16") as f:
            f.write("DONE")
    except Exception:
        pass


def _start_progress_ui(*, title: str, target_version: str) -> subprocess.Popen | None:
    """
    独立进度窗：脚本本身仅 ASCII；中文标题/文案从 UTF-16 元数据与状态文件读取。
    进度条为 0-100 确定进度。
    """
    status_path = _progress_status_path()
    script_path = _progress_script_path()
    meta_path = _progress_meta_path()
    _set_progress(5, "正在准备更新，请稍候…")

    ver = (target_version or "").strip()
    subtitle = f"正在安装新版本 {ver}" if ver else "正在安装新版本"
    try:
        os.makedirs(_app_data_dir(), exist_ok=True)
        with open(meta_path, "w", encoding="utf-16") as f:
            f.write(f"{title} - 正在更新\n")
            f.write(f"{subtitle}\n")
            f.write("请勿关闭本窗口或重新打开客户端\n")
    except Exception as e:
        _log(f"写入进度元数据失败: {e}")
        return None

    # 纯 ASCII 脚本，避免 .ps1 编码导致界面乱码
    status_ps = status_path.replace("'", "''")
    meta_ps = meta_path.replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$statusFile = '{status_ps}'
$metaFile = '{meta_ps}'
$meta = @(Get-Content -LiteralPath $metaFile -Encoding Unicode)
$winTitle = $meta[0]
$mainTitle = $meta[1]
$hintText = $meta[2]

$form = New-Object System.Windows.Forms.Form
$form.Text = $winTitle
$form.Width = 460
$form.Height = 190
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true
$form.ShowInTaskbar = $true

$lblTitle = New-Object System.Windows.Forms.Label
$lblTitle.AutoSize = $false
$lblTitle.Width = 420
$lblTitle.Height = 24
$lblTitle.Left = 16
$lblTitle.Top = 14
$lblTitle.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10, [System.Drawing.FontStyle]::Bold)
$lblTitle.Text = $mainTitle

$lblStatus = New-Object System.Windows.Forms.Label
$lblStatus.AutoSize = $false
$lblStatus.Width = 360
$lblStatus.Height = 36
$lblStatus.Left = 16
$lblStatus.Top = 44
$lblStatus.Text = '...'

$lblPct = New-Object System.Windows.Forms.Label
$lblPct.AutoSize = $false
$lblPct.Width = 56
$lblPct.Height = 24
$lblPct.Left = 380
$lblPct.Top = 48
$lblPct.TextAlign = 'MiddleRight'
$lblPct.Text = '0%'

$bar = New-Object System.Windows.Forms.ProgressBar
$bar.Left = 16
$bar.Top = 90
$bar.Width = 412
$bar.Height = 22
$bar.Minimum = 0
$bar.Maximum = 100
$bar.Style = 'Continuous'
$bar.Value = 0

$lblHint = New-Object System.Windows.Forms.Label
$lblHint.AutoSize = $false
$lblHint.Width = 420
$lblHint.Height = 20
$lblHint.Left = 16
$lblHint.Top = 122
$lblHint.ForeColor = [System.Drawing.Color]::DimGray
$lblHint.Text = $hintText

$form.Controls.Add($lblTitle)
$form.Controls.Add($lblStatus)
$form.Controls.Add($lblPct)
$form.Controls.Add($bar)
$form.Controls.Add($lblHint)

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 300
$timer.Add_Tick({{
  try {{
    if (-not (Test-Path -LiteralPath $statusFile)) {{ return }}
    $raw = Get-Content -LiteralPath $statusFile -Encoding Unicode -Raw -ErrorAction SilentlyContinue
    if ($null -eq $raw) {{ return }}
    $t = $raw.Trim()
    if ($t -eq 'DONE') {{
      $bar.Value = 100
      $lblPct.Text = '100%'
      $timer.Stop()
      $form.Close()
      return
    }}
    $idx = $t.IndexOf('|')
    if ($idx -lt 0) {{ return }}
    $pct = 0
    $ok = [int]::TryParse($t.Substring(0, $idx), [ref]$pct)
    if (-not $ok) {{ return }}
    if ($pct -lt 0) {{ $pct = 0 }}
    if ($pct -gt 100) {{ $pct = 100 }}
    $bar.Value = $pct
    $lblPct.Text = ($pct.ToString() + '%')
    if ($idx + 1 -lt $t.Length) {{
      $lblStatus.Text = $t.Substring($idx + 1)
    }}
  }} catch {{}}
}})
$timer.Start()
$form.Add_FormClosing({{
  if ($timer.Enabled) {{ $timer.Stop() }}
}})
[void]$form.ShowDialog()
"""
    try:
        # UTF-16 LE + BOM：Windows PowerShell 5.1 对中文脚本最稳；本脚本虽为 ASCII 也统一此编码
        with open(script_path, "w", encoding="utf-16") as f:
            f.write(script)
        proc = subprocess.Popen(  # nosec - 本地进度 UI
            [
                "powershell",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script_path,
            ],
        )
        _log("已启动更新进度窗口")
        return proc
    except Exception as e:
        _log(f"启动更新进度窗口失败: {e}")
        return None


def _close_progress_ui(progress_proc: subprocess.Popen | None) -> None:
    _set_progress_done()
    if progress_proc is None:
        return
    try:
        progress_proc.wait(timeout=8)
    except Exception:
        try:
            progress_proc.terminate()
        except Exception:
            pass


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
    started = time.time()
    while time.time() < deadline:
        if not _is_pid_running(pid):
            return True
        elapsed = time.time() - started
        pct = 8 + min(12, int(elapsed / max(timeout_sec, 1) * 12))
        _set_progress(pct, "正在关闭旧版本客户端…")
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
    started = time.time()
    while time.time() < deadline:
        if not any(_is_process_image_running(name) for name in _PROCESS_IMAGE_NAMES):
            return
        elapsed = time.time() - started
        pct = 20 + min(10, int(elapsed / max(timeout_sec, 1) * 10))
        _set_progress(pct, "正在确认程序已退出…")
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


def _dir_is_writable(path: str) -> bool:
    if not path:
        return False
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".mibuddy_write_test_{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _estimate_install_percent(started_at: float, log_path: str) -> int:
    """根据耗时与安装日志增长估算 35%~90% 安装进度。"""
    elapsed = max(0.0, time.time() - started_at)
    # 大体积 onefile 通常几十秒到几分钟；两分钟内爬到 90%
    time_pct = 35 + min(55, int(elapsed / 120.0 * 55))
    log_pct = 35
    try:
        if os.path.isfile(log_path):
            size = os.path.getsize(log_path)
            # 日志每增长约 8KB 约加 1%
            log_pct = 35 + min(55, size // 8192)
    except OSError:
        pass
    return max(35, min(90, max(time_pct, log_pct)))


def _run_installer_once(installer_path: str, *, elevated: bool) -> int:
    log_path = _install_log_path()
    try:
        if os.path.exists(log_path):
            os.remove(log_path)
    except Exception:
        pass

    cli_args = [*_INSTALLER_ARGS, f"/LOG={log_path}"]
    mode = "提权" if elevated else "普通"
    _log(f"启动安装包({mode}): {' '.join(cli_args)}")
    started = time.time()
    _set_progress(35, "正在安装新版本，请勿关闭…")

    if not elevated:
        proc = subprocess.Popen([installer_path, *cli_args])  # nosec
    else:
        installer_ps = installer_path.replace("'", "''")
        arg_ps = ",".join("'" + a.replace("'", "''") + "'" for a in cli_args)
        ps = (
            f"$p = Start-Process -FilePath '{installer_ps}' "
            f"-ArgumentList @({arg_ps}) -Verb RunAs -Wait -PassThru; "
            "if ($null -eq $p) { exit 1 }; exit $p.ExitCode"
        )
        proc = subprocess.Popen(  # nosec
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=_NO_WINDOW,
        )

    deadline = time.time() + 1800
    while proc.poll() is None:
        if time.time() > deadline:
            try:
                proc.kill()
            except Exception:
                pass
            return 1
        pct = _estimate_install_percent(started, log_path)
        _set_progress(pct, "正在安装新版本，请勿关闭…")
        time.sleep(0.5)

    return int(proc.returncode or 0)


def _run_installer(installer_path: str, *, app_exe_path: str = "") -> int:
    """只启动一次安装包，绝不因失败/取消再次拉起（避免第二窗 + 安全软件拦截）。"""
    if not os.path.isfile(installer_path):
        raise FileNotFoundError(f"安装包不存在: {installer_path}")

    install_dir = os.path.dirname(app_exe_path) if app_exe_path else ""
    need_elevate = bool(install_dir) and not _dir_is_writable(install_dir)
    _log(
        f"安装目录可写检测: dir={install_dir or '(未知)'} "
        f"writable={not need_elevate if install_dir else 'n/a'} elevate={need_elevate}"
    )

    code = _run_installer_once(installer_path, elevated=need_elevate)
    if code != 0:
        _append_install_log_tail()
    return code


def _launch_app_fallback(app_exe_path: str) -> None:
    if not app_exe_path or not os.path.isfile(app_exe_path):
        return
    try:
        if sys.platform.startswith("win"):
            os.startfile(app_exe_path)  # nosec
        else:
            subprocess.Popen([app_exe_path], close_fds=True)  # nosec
        _log(f"兜底拉起客户端: {app_exe_path}")
    except Exception as e:
        _log(f"兜底拉起客户端失败: {e}")


def _show_error(message: str) -> None:
    _log(message)
    try:
        safe = message.replace('"', '""').replace("\r", "").replace("\n", '" & vbCrLf & "')
        vbs = (
            f'Set ui = CreateObject("WScript.Shell")\n'
            f'ui.Popup "{safe}", 0, "更新失败", 16\n'
        )
        vbs_path = os.path.join(_app_data_dir(), "update_error.vbs")
        with open(vbs_path, "w", encoding="mbcs", errors="replace") as f:
            f.write(vbs)
        subprocess.run(
            ["wscript.exe", "//B", "//Nologo", vbs_path],
            creationflags=_NO_WINDOW,
            timeout=60,
        )
    except Exception:
        pass


def run_update(pending_path: str) -> int:
    progress_proc: subprocess.Popen | None = None
    try:
        pending = _read_pending(pending_path)
        installer_path = str(pending.get("installer_path") or "").strip()
        main_pid = int(pending.get("main_pid") or 0)
        app_exe_path = str(pending.get("app_exe_path") or "").strip()
        target_version = str(pending.get("target_version") or "").strip()

        _log(f"开始更新引导 target={target_version} main_pid={main_pid}")

        if not installer_path:
            raise ValueError("pending_update.json 缺少 installer_path")

        progress_proc = _start_progress_ui(
            title=_DISPLAY_NAME,
            target_version=target_version,
        )

        _set_progress(8, "正在关闭旧版本客户端…")
        _log(f"等待主进程退出 pid={main_pid}")
        exited = _wait_for_pid(main_pid, _WAIT_PID_TIMEOUT_SEC)
        if not exited:
            _log("等待主进程 PID 超时，将继续等待进程名退出")

        _set_progress(22, "正在确认程序已退出…")
        _wait_for_processes_stopped(_WAIT_PROCESS_STOP_SEC)
        if any(_is_process_image_running(name) for name in _PROCESS_IMAGE_NAMES):
            _log(f"仍有 {', '.join(_PROCESS_IMAGE_NAMES)} 在运行，安装可能失败")
        else:
            _log("目标进程已全部退出")

        time.sleep(_POST_EXIT_SETTLE_SEC)

        _set_progress(35, "正在安装新版本，请勿关闭…")
        code = _run_installer(installer_path, app_exe_path=app_exe_path)
        _log(f"安装包退出码: {code}")

        if code in _INNO_EXIT_USER_CANCEL:
            _set_progress(100, "更新已取消")
            _close_progress_ui(progress_proc)
            progress_proc = None
            _clear_update_lock()
            _log("更新已取消")
            return code

        if code != 0:
            _set_progress(100, "安装失败")
            _close_progress_ui(progress_proc)
            progress_proc = None
            hint = (
                f"安装未能完成（退出码 {code}）。\n\n"
                "常见原因：安装目录无写入权限、程序文件仍被占用、UAC 被拒绝。\n"
                f"详细日志：{_install_log_path()}\n\n"
                "可尝试手动运行安装包完成更新。"
            )
            _show_error(hint)
            _clear_update_lock()
            return code

        _set_progress(92, "正在完成收尾…")
        install_dir = os.path.dirname(app_exe_path) if app_exe_path else ""
        if install_dir:
            cleanup_legacy_install_files(install_dir)
            _log(f"已清理旧版安装文件: {install_dir}")

        migrate_legacy_user_data()
        _log("已合并旧版用户数据目录")

        _clear_update_lock()

        _set_progress(98, "更新完成，正在启动新版本…")
        time.sleep(1.0)
        if app_exe_path and not _is_process_image_running(os.path.basename(app_exe_path)):
            _launch_app_fallback(app_exe_path)

        _set_progress(100, "更新完成")
        time.sleep(0.4)
        _close_progress_ui(progress_proc)
        progress_proc = None
        _log("更新引导完成")
        return 0
    except Exception as e:
        _log(f"更新引导异常: {e}")
        _set_progress(100, "更新出现异常")
        _close_progress_ui(progress_proc)
        progress_proc = None
        _clear_update_lock()
        _show_error(f"更新过程出现异常：\n{e}")
        return 1
    finally:
        _close_progress_ui(progress_proc)


def default_pending_path() -> str:
    return os.path.join(_app_data_dir(), "pending_update.json")


def main() -> None:
    pending_path = sys.argv[1].strip() if len(sys.argv) > 1 else default_pending_path()
    code = run_update(pending_path)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
