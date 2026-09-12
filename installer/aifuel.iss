; Compile through build-installer.ps1. Keep AppId stable across releases.
#ifndef AppVersion
  #error AppVersion must be supplied by the build script
#endif
#ifndef PayloadDir
  #error PayloadDir must point to the complete PyInstaller onedir output
#endif
#ifndef InstallerAppId
  #define InstallerAppId "{{D9B828A2-8C5D-48AF-AB6F-F4390193D247}"
#endif
#ifndef InstallerMutex
  #define InstallerMutex "Local\aifuel_widget"
#endif
#ifndef InstallerName
  #define InstallerName "AI Fuel"
#endif
#ifndef InstallerStartupLinkName
  #define InstallerStartupLinkName "aifuel.lnk"
#endif

[Setup]
AppId={#InstallerAppId}
AppName={#InstallerName}
AppVersion={#AppVersion}
AppPublisher=er3s56
AppPublisherURL=https://github.com/er3s56/aifuel
DefaultDirName={localappdata}\Programs\{#InstallerName}
DefaultGroupName={#InstallerName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.17763
DisableProgramGroupPage=yes
DisableDirPage=no
WizardStyle=modern
SetupIconFile=..\aifuel.ico
UninstallDisplayIcon={app}\aifuel.exe
AppMutex={#InstallerMutex}
CloseApplications=yes
RestartApplications=no
OutputDir=..\dist\installer
OutputBaseFilename=AI-Fuel-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\{#InstallerName}"; Filename: "{app}\aifuel.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\{#InstallerName}"; Filename: "{app}\aifuel.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[InstallDelete]
; Retired payload owned by older AI Fuel installers; preserve user configuration.
Type: filesandordirs; Name: "{app}\_internal\taskbar_runtime"

[Run]
Filename: "{app}\aifuel.exe"; Parameters: "--autostart migrate ""{#InstallerStartupLinkName}"""; WorkingDir: "{app}"; Flags: runhidden
Filename: "{app}\aifuel.exe"; Description: "{cm:LaunchProgram,{#InstallerName}}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Shell, Folder, Shortcut: Variant;
  LinkPath, Target: String;
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  { The app creates this link itself. Only remove a link owned by this install. }
  LinkPath := ExpandConstant('{userstartup}\{#InstallerStartupLinkName}');
  if not FileExists(LinkPath) then
    Exit;
  try
    { WScript.Shell loses characters outside the system ANSI code page. }
    Shell := CreateOleObject('Shell.Application');
    Folder := Shell.NameSpace(ExtractFileDir(LinkPath));
    Shortcut := Folder.ParseName(ExtractFileName(LinkPath));
    Target := Shortcut.ExtendedProperty('System.Link.TargetParsingPath');
    if CompareText(Target, ExpandConstant('{app}\aifuel.exe')) = 0 then
      if not DeleteFile(LinkPath) then
        Log('Could not remove the startup shortcut.');
  except
    Log('Could not inspect the startup shortcut: ' + GetExceptionMessage);
  end;
end;
