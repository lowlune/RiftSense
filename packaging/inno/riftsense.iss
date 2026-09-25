; RiftSense per-user Inno Setup installer (Inno Setup 6.x).
;
; This is the phase-2 EXE packaging path. The portable ZIP remains the
; primary distribution; build the payload first:
;
;   1. powershell -NoProfile -ExecutionPolicy Bypass -File tools\build-release.ps1
;   2. Expand dist\RiftSense-win-x64-v<version>.zip so that dist\RiftSense exists
;   3. iscc packaging\inno\riftsense.iss /DAppVersion=<version>
;
; The installer writes only under %LOCALAPPDATA% and the user's Start Menu;
; it never requests admin (PrivilegesRequired=lowest). User state such as
; build_intent.txt, the timeline database and downloaded Data Dragon caches
; is excluded from [Files], so upgrades never overwrite it.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "RiftSense"
#define AppPublisher "lowlune"
#define AppURL "https://github.com/lowlune/RiftSense"
#define PayloadDir "..\..\dist\RiftSense"

[Setup]
AppId={{7C2E9A54-3B1D-4E8F-9A6B-2D5C8F1E4A70}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases/latest
DefaultDirName={localappdata}\{#AppName}
DisableProgramGroupPage=yes
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\..\dist
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0
UninstallDisplayName={#AppName} {#AppVersion}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut for the dashboard"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "build_intent.txt,build_intent.txt.bak,coach_latest.txt,coach_latest.json,death_latest.txt,death_latest.json,game_epoch.json,dashboard_token.txt,champion.json,items.json,update_state.json,last_run.tmp,*.log,*.err,*.pyc,__pycache__\*,ui\data\*,.update\*"

[Icons]
Name: "{group}\RiftSense UI"; Filename: "{app}\Start-Ui.cmd"; WorkingDir: "{app}"
Name: "{group}\RiftSense AutoCoach"; Filename: "{app}\Start-AutoCoach.cmd"; WorkingDir: "{app}"
Name: "{group}\Uninstall RiftSense"; Filename: "{uninstallexe}"
Name: "{autodesktop}\RiftSense UI"; Filename: "{app}\Start-Ui.cmd"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Start-Ui.cmd"; Description: "Start the RiftSense dashboard"; Flags: postinstall shellexec skipifsilent nowait

[UninstallDelete]
; The uninstaller removes files installed from [Files]. Runtime state that was
; excluded above (plans, timeline DB, downloaded caches) is left in place; this
; entry only cleans up a leftover updater marker.
Type: files; Name: "{app}\.update_in_progress"
