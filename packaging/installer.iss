; CanBot Windows kurulum programı (Inno Setup 6)
; Derleme: iscc packaging\installer.iss   (önce dist\CanBot.exe üretilmiş olmalı)

#define AppName "CanBot"
#define AppVersion "1.0.0"

[Setup]
AppId={{0E726587-C9AF-4451-92CC-2160AF7686BC}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=CanBot
DefaultDirName={localappdata}\Programs\CanBot
DefaultGroupName=CanBot
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=CanBot-Kurulum
SetupIconFile=..\bot\web\static\icons\canbot.ico
UninstallDisplayIcon={app}\CanBot.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[Tasks]
Name: "desktopicon"; Description: "Masaüstüne kısayol oluştur"; GroupDescription: "Ek seçenekler:"
Name: "autostart"; Description: "Windows açılınca CanBot'u simge durumunda başlat"; GroupDescription: "Ek seçenekler:"; Flags: unchecked

[Files]
Source: "..\dist\CanBot.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\CanBot"; Filename: "{app}\CanBot.exe"
Name: "{group}\CanBot'u kaldır"; Filename: "{uninstallexe}"
Name: "{userdesktop}\CanBot"; Filename: "{app}\CanBot.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "CanBot"; ValueData: """{app}\CanBot.exe"" --minimized"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\CanBot.exe"; Description: "CanBot'u şimdi başlat"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM CanBot.exe"; Flags: runhidden; RunOnceId: "StopCanBot"
