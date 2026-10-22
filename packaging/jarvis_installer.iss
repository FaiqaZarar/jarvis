; Windows installer for the desktop app, mirroring what the macOS build
; does with hdiutil/.dmg: wraps PyInstaller's raw onedir output
; (dist/jarvis_desktop/) into the format Windows users actually expect -
; a Start Menu entry and a real uninstaller - instead of a zipped folder
; they have to manually place themselves. Built with Inno Setup 6, which
; ships preinstalled on windows-latest GitHub Actions runners, so no
; extra install step is needed (same reasoning as macOS's hdiutil).
;
; Regenerate the compiled installer with:
;   ISCC jarvis_installer.iss
; (run from this directory, after `pyinstaller jarvis_desktop.spec` has
; produced dist/jarvis_desktop/).

#define AppName "Jarvis"
; Overridable via `ISCC /DAppVersion=... jarvis_installer.iss` - build.yml
; passes the CI run number so Add/Remove Programs shows which build is
; actually installed, instead of a constant that never changes.
#ifndef AppVersion
  #define AppVersion "0.1.0-dev"
#endif
#define AppExeName "jarvis_desktop.exe"

[Setup]
AppId={{6C7B6C8B-2C90-4E97-9F3D-6E7CE6E44B41}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; No admin password required to install, matching the macOS drag-to-
; Applications install - {autopf} resolves to the per-user Program
; Files equivalent under PrivilegesRequired=lowest.
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=JarvisSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; ignoreversion: without it, Inno Setup compares each file's own embedded
; version resource against what's already installed and silently skips
; an identical one - PyInstaller doesn't stamp a version resource on
; jarvis_desktop.exe at all, and several bundled DLLs are unmodified
; Windows runtime files that legitimately never change version, so this
; was skipping real, changed application files on every reinstall over
; an existing install (confirmed directly: "Same version. Skipping." in
; the installer's own /LOG output, for a build with real code changes).
Source: "dist\jarvis_desktop\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
