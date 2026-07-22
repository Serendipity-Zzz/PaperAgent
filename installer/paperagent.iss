#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif
#ifndef MyCommit
  #define MyCommit "development"
#endif

[Setup]
AppId={{CB796FC0-892B-4A69-A882-4199FB77CF87}
AppName=PaperAgent
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\PaperAgent
PrivilegesRequired=lowest
OutputDir=..\dist\release
OutputBaseFilename=PaperAgent-{#MyAppVersion}-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
Uninstallable=yes

[Files]
Source: "..\dist\release\PaperAgent-{#MyAppVersion}-windows-x64\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autodesktop}\PaperAgent"; Filename: "{app}\PaperAgent.exe"
Name: "{userprograms}\PaperAgent"; Filename: "{app}\PaperAgent.exe"

[Run]
Filename: "{app}\PaperAgent.exe"; Description: "启动 PaperAgent"; Flags: nowait postinstall skipifsilent
