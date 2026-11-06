#define AppName "Pocket Voice Clone Studio"
#define AppVersion "1.0.0"
#define AppPublisher "tbenitz"
#define AppExeName "VoiceCloneStudio.exe"

[Setup]
SourceDir=..
AppId={{964F1BD8-FA40-4EBC-BC2A-4137AC0299D3}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Pocket Voice Clone Studio
DefaultGroupName=Pocket Voice Clone Studio
OutputDir=installer-out
OutputBaseFilename=VoiceCloneStudio-Setup
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
SetupLogging=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}
ChangesAssociations=no
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no
LicenseFile=LICENSE

[Files]
Source: "dist\VoiceCloneStudio\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Pocket Voice Clone Studio"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\Pocket Voice Clone Studio"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a Desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch Pocket Voice Clone Studio"; Flags: nowait postinstall skipifsilent
