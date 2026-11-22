#ifndef MyVersion
  #define MyVersion "1.0.0"
#endif

[Setup]
AppId={{8C7D2E51-4B3A-4F7E-9C1D-B0D5EA11E5A1}
AppName=BudDeals
AppVersion={#MyVersion}
AppPublisher=Dougieee3D
DefaultDirName={localappdata}\Programs\BudDeals
DefaultGroupName=BudDeals
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=out
OutputBaseFilename=BudDeals-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
UninstallDisplayIcon={app}\BudDeals.exe

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\BudDeals\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\BudDeals"; Filename: "{app}\BudDeals.exe"
Name: "{userdesktop}\BudDeals"; Filename: "{app}\BudDeals.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\BudDeals.exe"; Description: "Open BudDeals"; Flags: nowait postinstall
