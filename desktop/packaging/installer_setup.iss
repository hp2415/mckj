; 米宝(Mibuddy) 专业安装包脚本 (整理版)
; 此脚本应在 desktop/packaging 目录下使用 Inno Setup 编译

#define MyAppDisplayName "米宝(Mibuddy)"
#define MyAppName "Mibuddy_Assistant"
#define MyAppVersion "1.0.11"
#define MyAppPublisher "米宝(Mibuddy)"
#define MyAppURL "http://192.168.0.100:8080"
#define MyAppExeName "Mibuddy_Assistant.exe"
#define LegacyAppExeName "WeChatAI_Assistant.exe"
#define LegacyUpdaterExeName "WeChatAI_Updater.exe"

[Setup]
AppId={{9ED45F2C-6B3C-4D2A-B981-EAB176310000}
AppName={#MyAppDisplayName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName=D:\{#MyAppName}
SetupIconFile=..\assets\mibuddy.ico
; 允许用户在正常安装时修改路径
DisableDirPage=no
; 开启路径记忆功能 (默认), 确保窗口打开即是旧路径
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
OutputDir=..\installer_dist
OutputBaseFilename=Mibuddy_Assistant_Setup
Compression=lzma
; 关闭固实压缩：大体积 PyInstaller onefile 在固实流中解压会长时间占用 CPU 并阻塞安装界面/任务栏
SolidCompression=no
WizardStyle=modern
; 尽量在更新时自动关闭旧进程，避免 exe 被占用导致 DeleteFile(5) 失败
CloseApplications=yes
; 仅关闭本客户端相关进程，避免 Restart Manager 扫描 *.exe 时牵连系统组件
CloseApplicationsFilter={#MyAppExeName},{#LegacyAppExeName},{#LegacyUpdaterExeName},Mibuddy_Updater.exe
RestartApplications=no
; 静默/自动更新时不弹「将要安装…」确认框（升级安装仍会显示安装向导与进度）
DisableStartupPrompt=yes
; 结束页「立即运行」若排队了重启替换文件，不要因此提示重启电脑
RestartIfNeededByRun=no
; 与 desktop/app_mutex.py 一致，便于 CloseApplications 结束旧客户端
AppMutex=Mibuddy.AppMutex
; 兼容旧版客户端进程识别（Inno 仅支持单个 AppMutex，旧 mutex 通过 CloseApplicationsFilter 关闭）
; 窗口启动时允许用户选择语言 (如果定义了多种语言)
ShowLanguageDialog=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\Chinese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; 品牌更名后覆盖安装：删除旧版可执行文件与快捷方式，避免与新文件并存
Type: files; Name: "{app}\{#LegacyAppExeName}"
Type: files; Name: "{app}\{#LegacyUpdaterExeName}"
Type: files; Name: "{autoprograms}\WeChatAI_Assistant.lnk"
Type: files; Name: "{autodesktop}\WeChatAI_Assistant.lnk"

[Files]
; 指向父目录下的 dist 文件夹
; PyInstaller onefile 已是压缩二进制，再 LZMA 几乎不减小体积却会显著拖慢解压并卡 UI；直接存储以加速安装
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion restartreplace nocompression
Source: "..\dist\Mibuddy_Updater.exe"; DestDir: "{app}"; Flags: ignoreversion nocompression
Source: "..\dist\config.ini"; DestDir: "{app}"; Flags: ignoreversion onlyifdoesntexist

[INI]
; 升级安装时强制写入新服务器地址（onlyifdoesntexist 会保留旧 config，此处覆盖 api_url）
Filename: "{app}\config.ini"; Section: "Network"; Key: "api_url"; String: "http://192.168.0.100:8080"

[Icons]
Name: "{autoprograms}\{#MyAppDisplayName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppDisplayName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 部分机器在安装结束页“立即运行”会偶发 PyInstaller onefile 的 python312.dll LoadLibrary 失败；
; 经验上多出现在安装器以管理员权限运行时。用原始用户上下文启动可显著降低概率。
; 静默更新后也要自动拉起新版（去掉 skipifsilent）
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppDisplayName, '&', '&&')}}"; Flags: nowait postinstall runasoriginaluser

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\desktop_cache"

[Messages]
; 降低小白重复启动安装包时的困惑
SetupAppRunningError=检测到 {#MyAppDisplayName} 仍在运行或上一次安装尚未结束。%n%n请先关闭其它客户端窗口，或等待当前安装完成。请勿重复打开安装程序或多次点击「安装」。
SetupAlreadyRunning=安装程序已在运行。请只保留一个安装窗口，等待进度完成，不要再次双击安装包。
