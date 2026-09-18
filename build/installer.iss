; Inno Setup 6 Script for Disk CleanUp
; docs/02-SPEC.md 9.2 & docs/03-PLAN.md P7

#ifndef MyAppVersion
#define MyAppVersion "2.1.0"
#endif
#define MyAppName "Disk CleanUp"
#define MyAppPublisher "Antigravity"
#define MyAppExeName "DiskCleanUp.exe"
#define MyAppId "{{A57E5779-1B6E-4CF6-896F-92DFEE26992F}}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Disk CleanUp
DefaultGroupName=Disk CleanUp
AllowNoIcons=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19044
Uninstallable=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
SetupIconFile=..\src\adc\ui\adc.ico
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir=..\dist
OutputBaseFilename=DiskCleanUp-Setup-{#MyAppVersion}-x64
DisableDirPage=no
DisableProgramGroupPage=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\DiskCleanUp\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
function NeedsWebView2(): Boolean;
var
  InstalledVersion: String;
begin
  Result := True;
  if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', InstalledVersion) then
  begin
    if Length(InstalledVersion) > 0 then
      Result := False;
  end;
  if Result and RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', InstalledVersion) then
  begin
    if Length(InstalledVersion) > 0 then
      Result := False;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    // SPEC 6.5: Remove Task Scheduler task if created
    Exec('schtasks.exe', '/Delete /TN "DiskCleanUp_AutoClean" /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('Ban co muon xoa toan bo du lieu cau hinh va bao cao don dep tai %LOCALAPPDATA%\DiskCleanUp khong?' + #13#10 + '(Do you want to delete all configuration and reports in %LOCALAPPDATA%\DiskCleanUp?)', mbConfirmation, MB_YESNO) = IDYES then
    begin
      DelTree(ExpandConstant('{localappdata}\DiskCleanUp'), True, True, True);
      DelTree(ExpandConstant('{appdata}\DiskCleanUp'), True, True, True);
    end;
  end;
end;

