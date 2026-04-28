# 1. 强制控制台使用 UTF-8 编码，解决中文乱码问题，无需再将脚本存为 GBK
$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try { chcp 65001 | Out-Null } catch { }

# 2. 使用 $PSScriptRoot 锚定路径
$Python = "$PSScriptRoot\..\..\.venv\Scripts\python.exe"
$MainScript = "$PSScriptRoot\..\main.py"
$PcaData = "$PSScriptRoot\..\pca.json"
$ConfigTemplate = "$PSScriptRoot\..\config.ini"
$UiDir = "$PSScriptRoot\..\ui"
$AssetsDir = "$PSScriptRoot\..\assets"
$AppIcon = "$PSScriptRoot\..\assets\mibuddy.ico"
$AppName = "WeChatAI_Assistant"

Write-Host "--- Build started (stable) ---" -ForegroundColor Cyan

# Check Python exists
if (-not (Test-Path $Python)) {
    Write-Error "Cannot find venv python: $Python"
    exit 1
}

# Run PyInstaller (array args avoids backtick issues)
$BasePrefix = & $Python -c "import sys; print(sys.base_prefix)"
$BasePrefix = ($BasePrefix | Select-Object -First 1).Trim()
$CondaBin = Join-Path $BasePrefix "Library\\bin"

$PyArgs = @(
    "-m", "PyInstaller",
    "--noconsole",
    "--onefile",
    "--distpath", "$PSScriptRoot\..\dist",
    "--workpath", "$PSScriptRoot\..\build",
    "--icon", "$AppIcon",
    "--add-data", "$PcaData;.",
    "--add-data", "$UiDir;ui",
    "--add-data", "$AssetsDir;assets",
    "--hidden-import", "qasync"
)

# Conda Python: _ctypes depends on ffi*.dll under Library/bin; add concrete files (wildcards in --add-binary may not expand)
if (Test-Path $CondaBin) {
    $FfiDlls = Get-ChildItem -Path $CondaBin -Filter "ffi*.dll" -File -ErrorAction SilentlyContinue
    foreach ($d in $FfiDlls) {
        $PyArgs += @("--add-binary", ("{0};." -f $d.FullName))
    }

    # sqlite3 on conda depends on sqlite3.dll + zlib.dll in Library/bin
    $SqliteDlls = Get-ChildItem -Path $CondaBin -Filter "sqlite*.dll" -File -ErrorAction SilentlyContinue
    foreach ($d in $SqliteDlls) {
        $PyArgs += @("--add-binary", ("{0};." -f $d.FullName))
    }
    $ZlibDlls = Get-ChildItem -Path $CondaBin -Filter "zlib*.dll" -File -ErrorAction SilentlyContinue
    foreach ($d in $ZlibDlls) {
        $PyArgs += @("--add-binary", ("{0};." -f $d.FullName))
    }

    # ssl on conda depends on OpenSSL DLLs under Library/bin
    $SslDlls = Get-ChildItem -Path $CondaBin -Filter "libssl*.dll" -File -ErrorAction SilentlyContinue
    foreach ($d in $SslDlls) {
        $PyArgs += @("--add-binary", ("{0};." -f $d.FullName))
    }
    $CryptoDlls = Get-ChildItem -Path $CondaBin -Filter "libcrypto*.dll" -File -ErrorAction SilentlyContinue
    foreach ($d in $CryptoDlls) {
        $PyArgs += @("--add-binary", ("{0};." -f $d.FullName))
    }
}

$PyArgs += @(
    "--clean",
    "--name", "$AppName",
    "$MainScript"
)

& $Python $PyArgs
if ($LASTEXITCODE -ne 0) {
    $ReqFile = Join-Path $PSScriptRoot "..\\requirements.txt"
    Write-Error ("PyInstaller failed (exit={0}). Install deps: {1} -m pip install -r {2} (or install pyinstaller)." -f $LASTEXITCODE, $Python, $ReqFile)
    exit $LASTEXITCODE
}

# Copy config template to dist
if (Test-Path $ConfigTemplate) {
    Write-Host "Copying config template..." -ForegroundColor Gray
    Copy-Item $ConfigTemplate -Destination "$PSScriptRoot\..\dist\config.ini" -Force
}

Write-Host "--- Build done ---" -ForegroundColor Green
Write-Host "Output: $PSScriptRoot\..\dist\$AppName.exe" -ForegroundColor Yellow
