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
Name: "modelmedium"; Description: "Download extra model: medium"; GroupDescription: "Optional online components:"; Flags: unchecked
Name: "modellargev3"; Description: "Download extra model: large-v3"; GroupDescription: "Optional online components:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{commonappdata}\{#AppName}\runtime"; Permissions: users-modify

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
Root: HKLM; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "{#AppName}"; ValueData: "Software\Clients\Media\{#AppName}\Capabilities"; Flags: uninsdeletevalue
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}"; ValueType: string; ValueName: ""; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#AppName}"
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Media player for video and audio files."
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mp4"; ValueData: "A1lPlayer.MediaFile"
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mkv"; ValueData: "A1lPlayer.MediaFile"
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".avi"; ValueData: "A1lPlayer.MediaFile"
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mov"; ValueData: "A1lPlayer.MediaFile"
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".wmv"; ValueData: "A1lPlayer.MediaFile"
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".flv"; ValueData: "A1lPlayer.MediaFile"
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".webm"; ValueData: "A1lPlayer.MediaFile"
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".m4v"; ValueData: "A1lPlayer.MediaFile"
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mp3"; ValueData: "A1lPlayer.MediaFile"
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".wav"; ValueData: "A1lPlayer.MediaFile"
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".flac"; ValueData: "A1lPlayer.MediaFile"
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".m4a"; ValueData: "A1lPlayer.MediaFile"
Root: HKLM; Subkey: "Software\Clients\Media\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".aac"; ValueData: "A1lPlayer.MediaFile"
Root: HKCR; Subkey: "A1lPlayer.MediaFile"; ValueType: string; ValueName: ""; ValueData: "{#AppName} media file"; Flags: uninsdeletekey
Root: HKCR; Subkey: "A1lPlayer.MediaFile\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"
Root: HKCR; Subkey: "A1lPlayer.MediaFile\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""
Root: HKCR; Subkey: ".mp4\OpenWithProgids"; ValueType: string; ValueName: "A1lPlayer.MediaFile"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCR; Subkey: ".mkv\OpenWithProgids"; ValueType: string; ValueName: "A1lPlayer.MediaFile"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCR; Subkey: ".avi\OpenWithProgids"; ValueType: string; ValueName: "A1lPlayer.MediaFile"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCR; Subkey: ".mov\OpenWithProgids"; ValueType: string; ValueName: "A1lPlayer.MediaFile"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCR; Subkey: ".wmv\OpenWithProgids"; ValueType: string; ValueName: "A1lPlayer.MediaFile"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCR; Subkey: ".flv\OpenWithProgids"; ValueType: string; ValueName: "A1lPlayer.MediaFile"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCR; Subkey: ".webm\OpenWithProgids"; ValueType: string; ValueName: "A1lPlayer.MediaFile"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCR; Subkey: ".m4v\OpenWithProgids"; ValueType: string; ValueName: "A1lPlayer.MediaFile"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCR; Subkey: ".mp3\OpenWithProgids"; ValueType: string; ValueName: "A1lPlayer.MediaFile"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCR; Subkey: ".wav\OpenWithProgids"; ValueType: string; ValueName: "A1lPlayer.MediaFile"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCR; Subkey: ".flac\OpenWithProgids"; ValueType: string; ValueName: "A1lPlayer.MediaFile"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCR; Subkey: ".m4a\OpenWithProgids"; ValueType: string; ValueName: "A1lPlayer.MediaFile"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCR; Subkey: ".aac\OpenWithProgids"; ValueType: string; ValueName: "A1lPlayer.MediaFile"; ValueData: ""; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\runtime\components"
Type: filesandordirs; Name: "{app}\runtime\huggingface"
Type: filesandordirs; Name: "{app}\runtime\models\faster-whisper-tiny"
Type: filesandordirs; Name: "{app}\runtime\models\faster-whisper-base"
Type: filesandordirs; Name: "{app}\runtime\models\faster-whisper-small"
Type: filesandordirs; Name: "{app}\runtime\models\faster-whisper-medium"
Type: filesandordirs; Name: "{app}\runtime\models\faster-whisper-large-v3"
Type: filesandordirs; Name: "{commonappdata}\{#AppName}"

[Code]
const
  OptionalComponentMessage =
    'A1lPlayer is installed with the offline base runtime: VLC, FFmpeg, faster-whisper, and small model. ' +
    'Optional model downloads require an internet connection and are stored in the shared runtime folder.';

function JsonEscape(Value: String): String;
begin
  Result := Value;
  StringChangeEx(Result, '\', '\\', True);
  StringChangeEx(Result, '"', '\"', True);
end;

function InstallWhisperModel(ModelSize: String): Boolean;
var
  TargetDir: String;
  RequestPath: String;
  StdoutPath: String;
  StderrPath: String;
  Payload: String;
  Params: String;
  ResultCode: Integer;
begin
  Result := True;
  TargetDir := ExpandConstant('{commonappdata}\{#AppName}\runtime\models\faster-whisper-') + ModelSize;
  RequestPath := ExpandConstant('{tmp}\a1lplayer-model-') + ModelSize + '.json';
  StdoutPath := ExpandConstant('{tmp}\a1lplayer-model-') + ModelSize + '.out';
  StderrPath := ExpandConstant('{tmp}\a1lplayer-model-') + ModelSize + '.err';
  Payload := '{"model_size":"' + JsonEscape(ModelSize) + '","install_target":"' + JsonEscape(TargetDir) + '"}';
  if not SaveStringToFile(RequestPath, Payload, False) then
  begin
    MsgBox('Could not prepare the optional model installer request for ' + ModelSize + '.', mbError, MB_OK);
    Result := False;
    exit;
  end;

  Params :=
    '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$p=Start-Process -FilePath ''' + ExpandConstant('{app}\{#AppExeName}') + ''' ' +
    '-ArgumentList ''--installer'',''whisper-model'' ' +
    '-RedirectStandardInput ''' + RequestPath + ''' ' +
    '-RedirectStandardOutput ''' + StdoutPath + ''' ' +
    '-RedirectStandardError ''' + StderrPath + ''' ' +
    '-Wait -PassThru; exit $p.ExitCode"';

  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    MsgBox('Could not start the optional model installer for ' + ModelSize + '.', mbError, MB_OK);
    Result := False;
    exit;
  end;

  if ResultCode <> 0 then
  begin
    MsgBox('Failed to install optional Whisper model "' + ModelSize + '". You can retry from inside A1lPlayer later.', mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    exit;

  if WizardIsTaskSelected('modeltiny') then
    InstallWhisperModel('tiny');
  if WizardIsTaskSelected('modelbase') then
    InstallWhisperModel('base');
  if WizardIsTaskSelected('modelmedium') then
    InstallWhisperModel('medium');
  if WizardIsTaskSelected('modellargev3') then
    InstallWhisperModel('large-v3');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if WizardIsTaskSelected('cuda') then
    MsgBox(OptionalComponentMessage + #13#10#13#10 + 'CUDA setup is still handled from inside the player when CUDA is selected.', mbInformation, MB_OK)
  else if WizardIsTaskSelected('modeltiny') or
          WizardIsTaskSelected('modelbase') or
          WizardIsTaskSelected('modelmedium') or
          WizardIsTaskSelected('modellargev3') then
    MsgBox(OptionalComponentMessage, mbInformation, MB_OK);
end;
