; Inno Setup script: MASON installer for Windows (x64). Compiled by the GitHub Actions workflow with ISCC.
; The installation directory is chosen by the user (default C:\Program Files\MASON); per-user data lives in %APPDATA%\MASON.
#define MyAppName "MASON"
#define MyAppVersion "1.0.2"
#define MyAppExeName "MASON.exe"
#ifndef SourceDir
  #define SourceDir "..\..\dist\MASON-1.0.2-windows-x64"
#endif

[Setup]
AppId={{7A9E8C2B-6C0B-4F4D-9B0E-3C5D1E2F7A61}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=MASON developers
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableDirPage=no
AllowNoIcons=yes
OutputDir=..\..\dist
OutputBaseFilename=MASON-{#MyAppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#MyAppExeName}
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} - configure model (--setup)"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--setup"
Name: "{group}\{#MyAppName} README"; Filename: "{app}\docs\README.md"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Start {#MyAppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\runtime\python\Lib\site-packages\__pycache__"
