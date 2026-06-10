#ifndef MyAppName
  #define MyAppName "Focale Relay"
#endif
#ifndef MyAppId
  #define MyAppId "11A3125E-D7EA-487D-9998-67E95343F4A5"
#endif
#ifndef MyExeName
  #define MyExeName "focale-relay.exe"
#endif
#ifndef MyDefaultDirName
  #define MyDefaultDirName "{autopf}\FocaleLocalRelay\Focale Relay"
#endif
#ifndef MySetupIconFile
  #define MySetupIconFile ""
#endif
#define MyAppPublisher "Focale"
#ifndef MyAppVersion
  #error "MyAppVersion must be provided by the build process."
#endif
#ifndef MySourceDir
  #define MySourceDir "dist\focale-relay"
#endif
#ifndef MyOutputDir
  #define MyOutputDir "dist\windows"
#endif

[Setup]
AppId={{{#MyAppId}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={#MyDefaultDirName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
Compression=lzma
SolidCompression=yes
OutputDir={#MyOutputDir}
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
WizardStyle=modern
#if MySetupIconFile != ""
SetupIconFile={#MySetupIconFile}
#endif

[Tasks]
Name: "modifypath"; Description: "Add {#MyAppName} to PATH"; GroupDescription: "Additional tasks:"

[Files]
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyExeName}"

[Code]
const
  EnvironmentKey = 'Environment';

function NeedsAddPath(Param: String): boolean;
var
  OrigPath: String;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', OrigPath) then
    OrigPath := '';
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;

procedure AddPath(Param: String);
var
  OrigPath: String;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', OrigPath) then
    OrigPath := '';
  if (OrigPath = '') then
    OrigPath := Param
  else
    OrigPath := OrigPath + ';' + Param;
  RegWriteStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', OrigPath);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('modifypath') then
  begin
    if NeedsAddPath(ExpandConstant('{app}')) then
      AddPath(ExpandConstant('{app}'));
  end;
end;
