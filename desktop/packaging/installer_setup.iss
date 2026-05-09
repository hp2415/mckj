; WeChatAI Assistant 专业安装包脚本 (整理版)
; 此脚本应在 desktop/packaging 目录下使用 Inno Setup 编译

#define MyAppName "WeChatAI_Assistant"
#define MyAppVersion "1.0.6"
#define MyAppPublisher "WeChatAI Team"
#define MyAppURL "http://192.168.0.193:8000"
#define MyAppExeName "WeChatAI_Assistant.exe"

[Setup]
AppId={{9ED45F2C-6B3C-4D2A-B981-EAB176310000}
AppName={#MyAppName}
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
OutputBaseFilename=WeChatAI_Assistant_Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
; 尽量在更新时自动关闭旧进程，避免 exe 被占用导致 DeleteFile(5) 失败
CloseApplications=yes
RestartApplications=no
; 窗口启动时允许用户选择语言 (如果定义了多种语言)
ShowLanguageDialog=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\Chinese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; 指向父目录下的 dist 文件夹
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion restartreplace
Source: "..\dist\config.ini"; DestDir: "{app}"; Flags: ignoreversion onlyifdoesntexist

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 部分机器在安装结束页“立即运行”会偶发 PyInstaller onefile 的 python312.dll LoadLibrary 失败；
; 经验上多出现在安装器以管理员权限运行时。用原始用户上下文启动可显著降低概率。
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\desktop_cache"
