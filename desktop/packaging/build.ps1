# 1. 强制控制台使用 UTF-8 编码，解决中文乱码问题，无需再将脚本存为 GBK
$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 2. 使用 $PSScriptRoot 锚定路径
$Python = "$PSScriptRoot\..\..\.venv\Scripts\python.exe"
$MainScript = "$PSScriptRoot\..\main.py"
$PcaData = "$PSScriptRoot\..\pca.json"
$ConfigTemplate = "$PSScriptRoot\..\config.ini"
$UiDir = "$PSScriptRoot\..\ui"
$AssetsDir = "$PSScriptRoot\..\assets"
$AppIcon = "$PSScriptRoot\..\assets\mibuddy.ico"
$AppName = "WeChatAI_Assistant"

Write-Host "--- 开始打包流程 (最终稳定版) ---" -ForegroundColor Cyan

# 检查 Python 是否存在
if (-not (Test-Path $Python)) {
    Write-Error "找不到虚拟环境中的 Python: $Python"
    exit 1
}

# 3. 执行打包命令 (使用数组封装参数，彻底解决反引号换行报错)
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
    "--hidden-import", "qasync",
    "--clean",
    "--name", "$AppName",
    "$MainScript"
)

& $Python $PyArgs

# 2. 自动分发配置文件模版到 dist 目录
if (Test-Path $ConfigTemplate) {
    Write-Host "正在分发配置文件模版..." -ForegroundColor Gray
    Copy-Item $ConfigTemplate -Destination "$PSScriptRoot\..\dist\config.ini" -Force
}

Write-Host "--- 打包完成 ---" -ForegroundColor Green
Write-Host "生成文件位于: d:\work_place\desktop\dist\$AppName.exe" -ForegroundColor Yellow
