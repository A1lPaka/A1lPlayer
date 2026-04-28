#define AppName "A1lPlayer"
#define AppVersion GetEnv("A1LPLAYER_VERSION")
#if AppVersion == ""
  #define AppVersion "0.1.0"
#endif
#define AppPublisher "Cute Alpaca Club"
#define AppExeName "A1lPlayer.exe"
#define SourceDir "..\dist\A1lPlayer"
#define OutputDir "..\release"

[Setup]
AppId={{9BDF3A38-045F-4E0F-B42F-A21F7669E571}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=A1lPlayerSetup-{#AppVersion}-win64
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\logo.ico
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "cuda"; Description: "Download NVIDIA CUDA acceleration runtime during setup"; GroupDescription: "Optional online components:"; Flags: unchecked
Name: "modeltiny"; Description: "Download extra model: tiny"; GroupDescription: "Optional online components:"; Flags: unchecked
Name: "modelbase"; Description: "Download extra model: base"; GroupDescription: "Optional online components:"; Flags: unchecked
Name: "modelsmall"; Description: "Download extra model: small"; GroupDescription: "Optional online components:"; Flags: unchecked
Name: "modellargev3"; Description: "Download extra model: large-v3"; GroupDescription: "Optional online components:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{commonappdata}\{#AppName}\runtime"; Permissions: users-modify

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\runtime\components"
Type: filesandordirs; Name: "{app}\runtime\huggingface"
Type: filesandordirs; Name: "{app}\runtime\models\faster-whisper-tiny"
Type: filesandordirs; Name: "{app}\runtime\models\faster-whisper-base"
Type: filesandordirs; Name: "{app}\runtime\models\faster-whisper-small"
Type: filesandordirs; Name: "{app}\runtime\models\faster-whisper-large-v3"
Type: filesandordirs; Name: "{commonappdata}\{#AppName}"

[Code]
const
  OptionalComponentMessage =
    'Optional online component downloads are not wired to release URLs yet. ' +
    'A1lPlayer was installed with the offline base runtime: VLC, FFmpeg, faster-whisper, and medium model. ' +
    'You can download optional CUDA/models later from inside the player once component URLs are published.';

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if WizardIsTaskSelected('cuda') or
     WizardIsTaskSelected('modeltiny') or
     WizardIsTaskSelected('modelbase') or
     WizardIsTaskSelected('modelsmall') or
     WizardIsTaskSelected('modellargev3') then
  begin
    MsgBox(OptionalComponentMessage, mbInformation, MB_OK);
  end;
end;
