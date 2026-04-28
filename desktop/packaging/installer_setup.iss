; WeChatAI Assistant 专业安装包脚本 (整理版)
; 此脚本应在 desktop/packaging 目录下使用 Inno Setup 编译

#define MyAppName "WeChatAI_Assistant"
#define MyAppVersion "1.0.2"
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
DefaultDirName={autopf}\{#MyAppName}
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
; 窗口启动时允许用户选择语言 (如果定义了多种语言)
ShowLanguageDialog=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\Chinese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; 指向父目录下的 dist 文件夹
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\config.ini"; DestDir: "{app}"; Flags: ignoreversion onlyifdoesntexist

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\desktop_cache"
