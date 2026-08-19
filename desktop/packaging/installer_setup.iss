; 米宝(Mibuddy) 专业安装包脚本 (整理版)
; 此脚本应在 desktop/packaging 目录下使用 Inno Setup 编译

#define MyAppDisplayName "米宝(Mibuddy)"
#define MyAppName "Mibuddy_Assistant"
#define MyAppVersion "1.0.20"
#define MyAppPublisher "米宝(Mibuddy)"
#define MyAppURL "https://mibuddy.micheng.cn"
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
; 关闭 Restart Manager：部分 Win10/11 上 CloseApplications 会卡死任务栏。
; 旧引导器仍可能传入 /CLOSEAPPLICATIONS；用永不匹配的过滤规则使其找不到可关闭目标。
CloseApplications=no
CloseApplicationsFilter=.__mibuddy_no_close__
RestartApplications=no
; 静默/自动更新时不弹「将要安装…」确认框（手动双击安装包仍显示向导）
DisableStartupPrompt=yes
; 结束页「立即运行」若排队了重启替换文件，不要因此提示重启电脑
RestartIfNeededByRun=no
; 与 desktop/app_mutex.py 一致：检测到旧客户端仍在运行时提示关闭（不走 Restart Manager）
AppMutex=Mibuddy.AppMutex
; 兼容旧版客户端进程识别（Inno 仅支持单个 AppMutex）
ShowLanguageDialog=yes
PrivilegesRequired=admin

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\Chinese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; 品牌更名后覆盖安装：删除旧版可执行文件与快捷方式，避免与新文件并存
; 注意：不要在此删除 *.old —— 旧版引导器可能仍从重命名后的路径运行，删除会弹错
Type: files; Name: "{app}\{#LegacyAppExeName}"
Type: files; Name: "{app}\{#LegacyUpdaterExeName}"
Type: files; Name: "{autoprograms}\WeChatAI_Assistant.lnk"
Type: files; Name: "{autodesktop}\WeChatAI_Assistant.lnk"

[Files]
; 指向父目录下的 dist 文件夹
; PyInstaller onefile 已是压缩二进制，再 LZMA 几乎不减小体积却会显著拖慢解压并卡 UI；直接存储以加速安装
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion restartreplace nocompression
; restartreplace：若仍被占用则排队重启替换，避免 DeleteFile(5) 硬失败
Source: "..\dist\Mibuddy_Updater.exe"; DestDir: "{app}"; Flags: ignoreversion restartreplace nocompression
Source: "..\dist\config.ini"; DestDir: "{app}"; Flags: ignoreversion onlyifdoesntexist

[INI]
; 升级安装时强制写入新服务器地址（onlyifdoesntexist 会保留旧 config，此处覆盖 api_url）
Filename: "{app}\config.ini"; Section: "Network"; Key: "api_url"; String: "https://mibuddy.micheng.cn"

[Icons]
Name: "{autoprograms}\{#MyAppDisplayName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppDisplayName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 部分机器在安装结束页“立即运行”会偶发 PyInstaller onefile 的 python312.dll LoadLibrary 失败；
; 经验上多出现在安装器以管理员权限运行时。用原始用户上下文启动可显著降低概率。
; 自动更新为 /VERYSILENT，由 Mibuddy_Updater 以普通用户拉起，避免提权安装后任务栏异常。
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppDisplayName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\desktop_cache"

[Code]
(* 旧版 Updater 会传 /CLOSEAPPLICATIONS：
   1) Restart Manager 可能卡死任务栏 → 用永不匹配的 CloseApplicationsFilter 实质上禁用
   2) 用户取消后旧 Updater 会 PowerShell 二次提权再弹窗 → 检测到该参数则跳过向导并禁用取消 *)
function HasBadCloseAppsArg: Boolean;
var
  i: Integer;
  p: String;
begin
  Result := False;
  for i := 1 to ParamCount do
  begin
    p := ParamStr(i);
    if (CompareText(p, '/CLOSEAPPLICATIONS') = 0) or
       (CompareText(p, '/FORCECLOSEAPPLICATIONS') = 0) then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  (* 旧引导器自动更新：跳过可取消的向导页，直接进入安装 *)
  Result := HasBadCloseAppsArg and
    (PageID <> wpPreparing) and
    (PageID <> wpInstalling);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if not HasBadCloseAppsArg then
    Exit;
  WizardForm.CancelButton.Enabled := False;
  WizardForm.CancelButton.Visible := False;
  if CurPageID = wpReady then
    WizardForm.NextButton.OnClick(WizardForm.NextButton);
end;

(* 运行中的 exe 通常无法删除/覆盖，但可以重命名。
   旧版从安装目录的 Updater 拉起更新时，安装前先挪开占用文件，避免 DeleteFile(5)。 *)
procedure RenameAsideIfExists(const FilePath: String);
var
  BakPath: String;
begin
  if not FileExists(FilePath) then
    Exit;
  BakPath := FilePath + '.old';
  if FileExists(BakPath) then
    DeleteFile(BakPath);
  if not RenameFile(FilePath, BakPath) then
    Log('RenameAsideIfExists failed: ' + FilePath)
  else
    Log('Renamed locked/in-use file aside: ' + FilePath + ' -> ' + BakPath);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  NeedsRestart := False;
  Result := '';
  RenameAsideIfExists(ExpandConstant('{app}\Mibuddy_Updater.exe'));
  RenameAsideIfExists(ExpandConstant('{app}\{#LegacyUpdaterExeName}'));
end;

[Messages]
; 降低小白重复启动安装包时的困惑
SetupAppRunningError=检测到 {#MyAppDisplayName} 仍在运行或上一次安装尚未结束。%n%n请先关闭其它客户端窗口，或等待当前安装完成。请勿重复打开安装程序或多次点击「安装」。
SetupAlreadyRunning=安装程序已在运行。请只保留一个安装窗口，等待进度完成，不要再次双击安装包。
