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
$UpdaterSpec = "$PSScriptRoot\..\WeChatAI_Updater.spec"
$UpdaterBootstrap = "$PSScriptRoot\..\update_bootstrap.py"
$DistDir = "$PSScriptRoot\..\dist"
$UpdaterExe = Join-Path $DistDir "WeChatAI_Updater.exe"

Write-Host "--- Build started (stable) ---" -ForegroundColor Cyan

# Check Python exists
if (-not (Test-Path $Python)) {
    Write-Error "Cannot find venv python: $Python"
    exit 1
}

$BuildScript = $MyInvocation.MyCommand.Path

function Add-CondaRuntimeBinaries {
    param([Parameter(Mandatory = $true)][ref]$PyArgsRef)

    if (-not (Test-Path $script:CondaBin)) { return }

    $patterns = @("ffi*.dll", "sqlite*.dll", "zlib*.dll", "libssl*.dll", "libcrypto*.dll")
    foreach ($pattern in $patterns) {
        $dlls = Get-ChildItem -Path $script:CondaBin -Filter $pattern -File -ErrorAction SilentlyContinue
        foreach ($d in $dlls) {
            $PyArgsRef.Value += @("--add-binary", ("{0};." -f $d.FullName))
        }
    }
}

function Invoke-UpdaterBuildIfNeeded {
    $needBuild = $false
    if (-not (Test-Path $UpdaterExe)) {
        Write-Host "Updater not found in dist, will build..." -ForegroundColor Yellow
        $needBuild = $true
    } elseif ((Get-Item $UpdaterBootstrap).LastWriteTime -gt (Get-Item $UpdaterExe).LastWriteTime) {
        Write-Host "update_bootstrap.py changed, will rebuild updater..." -ForegroundColor Yellow
        $needBuild = $true
    } elseif ((Get-Item $UpdaterSpec).LastWriteTime -gt (Get-Item $UpdaterExe).LastWriteTime) {
        Write-Host "WeChatAI_Updater.spec changed, will rebuild updater..." -ForegroundColor Yellow
        $needBuild = $true
    } elseif ((Get-Item $BuildScript).LastWriteTime -gt (Get-Item $UpdaterExe).LastWriteTime) {
        Write-Host "build.ps1 changed, will rebuild updater..." -ForegroundColor Yellow
        $needBuild = $true
    } else {
        Write-Host "Skip updater build (dist\WeChatAI_Updater.exe is up to date)" -ForegroundColor Gray
    }

    if (-not $needBuild) { return }

    Write-Host "Building WeChatAI_Updater.exe ..." -ForegroundColor Cyan
    $UpdaterPyArgs = @(
        "-m", "PyInstaller",
        "--noconsole",
        "--onefile",
        "--distpath", $DistDir,
        "--workpath", "$PSScriptRoot\..\build\WeChatAI_Updater",
        "--icon", $AppIcon,
        "--clean",
        "--name", "WeChatAI_Updater",
        $UpdaterBootstrap
    )
    Add-CondaRuntimeBinaries ([ref]$UpdaterPyArgs)
    & $Python $UpdaterPyArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Updater PyInstaller failed (exit=$LASTEXITCODE)"
        exit $LASTEXITCODE
    }
}

$BasePrefix = & $Python -c "import sys; print(sys.base_prefix)"
$BasePrefix = ($BasePrefix | Select-Object -First 1).Trim()
$script:CondaBin = Join-Path $BasePrefix "Library\\bin"

Invoke-UpdaterBuildIfNeeded

# Run PyInstaller (array args avoids backtick issues)
$PyArgs = @(
    "-m", "PyInstaller",
    "--noconsole",
    "--onefile",
    "--distpath", $DistDir,
    "--workpath", "$PSScriptRoot\..\build",
    "--icon", "$AppIcon",
    "--add-data", "$PcaData;.",
    "--add-data", "$UiDir;ui",
    "--add-data", "$AssetsDir;assets",
    "--hidden-import", "qasync"
)

Add-CondaRuntimeBinaries ([ref]$PyArgs)

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
