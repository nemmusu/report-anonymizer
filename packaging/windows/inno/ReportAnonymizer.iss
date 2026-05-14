; ReportAnonymizer.iss
;
; Inno Setup 6 script for the Report Anonymizer Windows installer.
; Authored per the canonical plan (Fase 2.4 / 2.4.1 / 2.4.1.bis).
;
; The version string is injected at compile time by packaging/windows/build.ps1:
;     iscc.exe /DMyAppVersion=1.0.0 /DStagingDir=...\staging ReportAnonymizer.iss
;
; Required defines (passed via /D... on the ISCC.exe command line):
;     MyAppVersion          - "1.0.0", read from pyproject.toml by build.ps1.
;     StagingDir            - absolute path to <repo>\packaging\windows\staging.
;     OutputDir             - absolute path to <repo>\packaging\windows\dist.
;     OutputBaseFilename    - "Report-Anonymizer-Setup-x64-1.0.0" (or -lean).
;
; Optional defines:
;     IncludeCuda           - "1" to include the CUDA variant in [Files], "0" otherwise.
;     IncludeVulkan         - "1" to include the Vulkan variant in [Files], "0" otherwise.
;
; The AppId is a STABLE UUID. Do NOT change it across releases (Inno relies on
; it to detect upgrades vs. side-by-side installs).

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef StagingDir
  #define StagingDir "..\staging"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef OutputBaseFilename
  #define OutputBaseFilename "Report-Anonymizer-Setup-x64-" + MyAppVersion
#endif
#ifndef IncludeCuda
  #define IncludeCuda "1"
#endif
#ifndef IncludeVulkan
  #define IncludeVulkan "1"
#endif

#define MyAppName        "Report Anonymizer"
#define MyAppPublisher   "nemmusu"
#define MyAppURL         "https://github.com/nemmusu/report-anonymizer"
#define MyAppExeName     "ReportAnonymizer.exe"

[Setup]
AppId={{0B7F8C6F-1E2B-4B7C-9E3F-2026A0A100A1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases

DefaultDirName={localappdata}\Programs\report-anonymizer
DefaultGroupName=Report Anonymizer

PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
MinVersion=10.0.17763
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Conservative upper-bound for the unpacked footprint (700 MB).
; Per-component disk space is handled via ExtraDiskSpaceRequired on each
; [Components] entry; there is no DiskSpacePerComponentMode directive in
; Inno Setup 6.x.
ExtraDiskSpaceRequired=734003200

; ISCC 6.x is a 32-bit process; lzma2/ultra64 uses a 1 GB dictionary which can
; exhaust the address space when the staging payload exceeds ~1.5 GB and the
; worker is also 32-bit. lzma2/max uses a 256 MB dictionary, leaves ample room
; for I/O buffers, and only loses ~3-5% compression ratio.
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes

SetupIconFile=..\launcher\app_icon.ico
UninstallDisplayIcon={app}\launcher\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

CloseApplications=force
RestartApplications=no

UsePreviousAppDir=yes
UsePreviousLanguage=yes
UsePreviousTasks=yes

OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}

WizardStyle=modern
ShowLanguageDialog=auto

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Components]
Name: "core"; Description: "Application core (required)"; Types: full compact custom; Flags: fixed
Name: "cpu"; Description: "llama.cpp CPU runtime (AVX2, ~10 MB) - fallback, works on any x86_64"; Types: full compact custom; Flags: fixed
#if IncludeCuda == "1"
Name: "cuda"; Description: "llama.cpp CUDA 12 runtime (~150 MB) - NVIDIA GPUs"; Types: full
#endif
#if IncludeVulkan == "1"
Name: "vulkan"; Description: "llama.cpp Vulkan runtime (~30 MB) - NVIDIA/AMD/Intel via Vulkan"; Types: full
#endif

[Files]
; -----------------------------------------------------------------------------
; Always-installed payload.
; -----------------------------------------------------------------------------
Source: "{#StagingDir}\app\python\*";   DestDir: "{app}\python";   Flags: recursesubdirs createallsubdirs ignoreversion; Components: core
Source: "{#StagingDir}\app\runtime\*";  DestDir: "{app}\runtime";  Flags: recursesubdirs createallsubdirs ignoreversion; Components: core
Source: "{#StagingDir}\app\tools\*";    DestDir: "{app}\tools";    Flags: recursesubdirs createallsubdirs ignoreversion; Components: core
Source: "{#StagingDir}\app\repo\*";     DestDir: "{app}\repo";     Flags: recursesubdirs createallsubdirs ignoreversion; Components: core
Source: "{#StagingDir}\app\launcher\*"; DestDir: "{app}\launcher"; Flags: recursesubdirs createallsubdirs ignoreversion; Components: core

; -----------------------------------------------------------------------------
; llama.cpp variants. Each is gated by its Component, AND a Check function
; (ShouldCopyVariant) so that the user's radio choice in our custom page
; overrides the default component selection.
;
; Inno extracts both the per-variant tree under app\llama-variants\<v>\ and
; (for the chosen variant only) directly into app\tools\ so that the runtime
; can call llama-server without knowing which variant won.
; -----------------------------------------------------------------------------
Source: "{#StagingDir}\app\llama-variants\cpu\*"; DestDir: "{app}\llama-variants\cpu"; Flags: recursesubdirs createallsubdirs ignoreversion; Components: cpu
Source: "{#StagingDir}\app\llama-variants\cpu\*"; DestDir: "{app}\tools";              Flags: recursesubdirs createallsubdirs ignoreversion; Components: cpu; Check: ShouldCopyVariant('cpu')

#if IncludeCuda == "1"
Source: "{#StagingDir}\app\llama-variants\cuda\*"; DestDir: "{app}\llama-variants\cuda"; Flags: recursesubdirs createallsubdirs ignoreversion; Components: cuda
Source: "{#StagingDir}\app\llama-variants\cuda\*"; DestDir: "{app}\tools";               Flags: recursesubdirs createallsubdirs ignoreversion; Components: cuda; Check: ShouldCopyVariant('cuda')
#endif
#if IncludeVulkan == "1"
Source: "{#StagingDir}\app\llama-variants\vulkan\*"; DestDir: "{app}\llama-variants\vulkan"; Flags: recursesubdirs createallsubdirs ignoreversion; Components: vulkan
Source: "{#StagingDir}\app\llama-variants\vulkan\*"; DestDir: "{app}\tools";                 Flags: recursesubdirs createallsubdirs ignoreversion; Components: vulkan; Check: ShouldCopyVariant('vulkan')
#endif

[Icons]
Name: "{group}\{#MyAppName}";          Filename: "{app}\launcher\{#MyAppExeName}"; IconFilename: "{app}\launcher\app_icon.ico"
Name: "{group}\{#MyAppName} (CLI)";    Filename: "cmd.exe"; Parameters: "/k ""{app}\launcher\report-anonymizer-cli.exe"" --help"; IconFilename: "{app}\launcher\app_icon.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
; PrivilegesRequired=lowest blocks writes to {commondesktop} (C:\Users\Public\Desktop)
; with 0x80070005 Access Denied. {userdesktop} writes to the current user's Desktop
; and matches the per-user install model.
Name: "{userdesktop}\{#MyAppName}";    Filename: "{app}\launcher\{#MyAppExeName}"; IconFilename: "{app}\launcher\app_icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\launcher\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only clean up runtime-generated artefacts that Inno doesn't track
; via the [Files] manifest. The install tree itself (python/, repo/,
; tools/, runtime/, llama-variants/, launcher/) is intentionally NOT
; listed here: the keep-files prompt in CurUninstallStepChanged is
; the single source of truth for what gets removed, and the final
; ``DelTree({app})`` sweep there handles the install root once the
; user has actually agreed to remove it. Listing the subtrees here
; would defeat the keep-files choice — they would vanish before the
; prompt ever fired.
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\__pycache__"
Type: filesandordirs; Name: "{app}\tools\__pycache__"

; ===========================================================================
; [Code] Pascal Script
;
; Implements:
;   - Hardware detection (lightweight WMI query, no Python dependency at
;     wizard time).
;   - Custom "Choose llama.cpp variant" wizard page with radio buttons.
;   - Existing-install detection ("Keep existing" option).
;   - Backup of llama-server.exe.bak before overwrite.
;   - Post-install --version smoke test + CPU fallback prompt on failure.
;   - Sentinel JSON  (%APPDATA%\report-anonymizer\.installer_choice.json).
;   - server.yml seed  (preserving custom profiles).
;   - Uninstaller user-data removal prompt.
;   - Silent install support (/SILENT /VERYSILENT /CPU /CUDA /VULKAN /SKIP).
;
; The Pascal Script "language" Inno uses is a subset of Delphi; no generics,
; no anonymous methods. Most file IO must go through LoadStringFromFile /
; SaveStringToUTF8File rather than TFileStream.
; ===========================================================================
[Code]

const
  BACKSLASH        = #92;
  HW_TIMEOUT_MS    = 5000;
  SMOKE_TIMEOUT_MS = 5000;

var
  VariantPage:    TWizardPage;
  RadioCPU:       TNewRadioButton;
  RadioCUDA:      TNewRadioButton;
  RadioVulkan:    TNewRadioButton;
  RadioSkip:      TNewRadioButton;
  RadioKeep:      TNewRadioButton;
  HwInfoLabel:    TNewStaticText;
  RecommendLabel: TNewStaticText;

  SelectedVariant:  String;   { 'cpu' | 'cuda' | 'vulkan' | 'skip' | 'keep' }
  DetectedBackend:  String;   { 'nvidia' | 'amd' | 'intel' | '' }
  DetectedGpuName:  String;
  KeepExistingPath: String;
  KeepExistingTag:  String;   { 'cpu' | 'cuda' | 'vulkan' if detected, else '' }
  VariantPageInitialized: Boolean;

{ --------------------------------------------------------------------------
  Utility helpers.
  -------------------------------------------------------------------------- }

function StringContains(const Haystack, Needle: String): Boolean;
begin
  Result := Pos(LowerCase(Needle), LowerCase(Haystack)) > 0;
end;

function ReadTextFile(const Path: String): String;
var
  S: AnsiString;
begin
  Result := '';
  if not FileExists(Path) then Exit;
  if LoadStringFromFile(Path, S) then
    Result := String(S);
end;

function WriteTextFileUtf8(const Path, Body: String): Boolean;
var
  Dir: String;
begin
  Result := False;
  Dir := ExtractFileDir(Path);
  if (Dir <> '') and not DirExists(Dir) then
    ForceDirectories(Dir);
  try
    { Inno Setup 6.7.1 has no singular SaveStringToUTF8File. SaveStringToFile  }
    { takes an AnsiString, and UTF8Encode() returns the UTF-8 byte sequence    }
    { for a Unicode String. We deliberately write WITHOUT a BOM because the    }
    { consumers (JSON / YAML) prefer no-BOM and most JSON parsers reject one.  }
    Result := SaveStringToFile(Path, UTF8Encode(Body), False);
  except
    Result := False;
  end;
end;

function PadHex4(N: Integer): String;
var
  H, Digits: String;
  V: Integer;
begin
  { Inno Setup's Pascal Script has neither IntToHex nor IntToStr-with-radix, }
  { so we compute the 4-character zero-padded hex representation manually.   }
  Digits := '0123456789ABCDEF';
  H := '';
  V := N;
  if V < 0 then V := 0;
  while V > 0 do begin
    H := Digits[(V mod 16) + 1] + H;
    V := V div 16;
  end;
  while Length(H) < 4 do H := '0' + H;
  Result := H;
end;

function GetUtcIsoNow(): String;
begin
  { Pascal Script in Inno Setup 6 does not expose TSystemTime / GetSystemTime, }
  { but GetDateTimeString is available. We build a local-time ISO-8601 stamp;  }
  { the trailing Z is a (white) lie acceptable here because the sentinel JSON  }
  { is only consumed by the same machine and we don't depend on tz precision. }
  Result := GetDateTimeString('yyyy/mm/dd"T"hh:nn:ss"Z"', '-', ':');
end;

function JsonEscape(const S: String): String;
var
  i, code: Integer;
  c: Char;
begin
  Result := '';
  for i := 1 to Length(S) do begin
    c := S[i];
    code := Ord(c);
    if code = 92 then           { backslash }
      Result := Result + BACKSLASH + BACKSLASH
    else if code = 34 then      { double quote }
      Result := Result + BACKSLASH + '"'
    else if code = 8 then
      Result := Result + BACKSLASH + 'b'
    else if code = 9 then
      Result := Result + BACKSLASH + 't'
    else if code = 10 then
      Result := Result + BACKSLASH + 'n'
    else if code = 12 then
      Result := Result + BACKSLASH + 'f'
    else if code = 13 then
      Result := Result + BACKSLASH + 'r'
    else if code < 32 then
      Result := Result + BACKSLASH + 'u' + PadHex4(code)
    else
      Result := Result + c;
  end;
end;

function YamlEscapePath(const S: String): String;
{ Escape a Windows path for a YAML double-quoted scalar:
  backslashes doubled, embedded double quotes backslash-escaped. }
var
  i, code: Integer;
  c: Char;
begin
  Result := '';
  for i := 1 to Length(S) do begin
    c := S[i];
    code := Ord(c);
    if code = 92 then
      Result := Result + BACKSLASH + BACKSLASH
    else if code = 34 then
      Result := Result + BACKSLASH + '"'
    else
      Result := Result + c;
  end;
end;

{ --------------------------------------------------------------------------
  Silent-install parameter parsing.
  -------------------------------------------------------------------------- }

function HasCmdLineParam(const Name: String): Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to ParamCount do begin
    if CompareText(ParamStr(i), Name) = 0 then begin
      Result := True;
      Exit;
    end;
  end;
end;

function GetSilentVariant(): String;
begin
  if HasCmdLineParam('/CUDA')        then Result := 'cuda'
  else if HasCmdLineParam('/VULKAN') then Result := 'vulkan'
  else if HasCmdLineParam('/SKIP')   then Result := 'skip'
  else                                    Result := 'cpu';
end;

{ --------------------------------------------------------------------------
  Recursive directory copy (Inno's FileCopy is single-file only).
  Used by the CPU fallback path.
  -------------------------------------------------------------------------- }

procedure CopyDirRecursive(const Src, Dst: String);
var
  FindRec: TFindRec;
  SrcSub, DstSub: String;
begin
  if not DirExists(Src) then Exit;
  if not DirExists(Dst) then ForceDirectories(Dst);

  if FindFirst(AddBackslash(Src) + '*', FindRec) then begin
    try
      repeat
        if (FindRec.Name = '.') or (FindRec.Name = '..') then Continue;
        SrcSub := AddBackslash(Src) + FindRec.Name;
        DstSub := AddBackslash(Dst) + FindRec.Name;
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
          CopyDirRecursive(SrcSub, DstSub)
        else
          FileCopy(SrcSub, DstSub, False);
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

procedure CopyVariantToTools(const Variant: String);
var
  Src, Dst: String;
begin
  Src := ExpandConstant('{app}') + BACKSLASH + 'llama-variants' + BACKSLASH + Variant;
  Dst := ExpandConstant('{app}') + BACKSLASH + 'tools';
  if not DirExists(Src) then Exit;
  CopyDirRecursive(Src, Dst);
end;

{ --------------------------------------------------------------------------
  Hardware detection at wizard time.

  The Python embed is not yet extracted when the wizard runs, so we use a
  lightweight WMI query via wmic (preinstalled on Win 10/11) to identify
  the primary GPU vendor. The thorough Python-side detection
  (anonymize.hardware.report_dict) runs as part of the smoke-test step
  AFTER ssInstall, where the staged Python tree is on disk.
  -------------------------------------------------------------------------- }

// ---------------------------------------------------------------------------
// Multi-strategy GPU detection.
//
// Win11 24H2+ ships without wmic (Microsoft deprecated it and the
// "WMIC" optional feature is no longer installed by default), so the
// previous wmic-only path was returning empty on modern systems even when
// nvidia-smi was on PATH and a discrete GPU was present. We try, in order:
//   1. nvidia-smi  -> highest confidence (driver installed AND working).
//   2. PowerShell Get-CimInstance Win32_VideoController (modern, present on
//      every supported Windows 10/11 SKU including Server Core).
//   3. wmic Win32_VideoController (legacy fallback).
// ---------------------------------------------------------------------------

function ExecToFile(const Exe, CmdLine, OutFile: String; var ExitCode: Integer): Boolean;
begin
  if FileExists(OutFile) then DeleteFile(OutFile);
  Result := Exec(Exe, CmdLine, '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
end;

function ClassifyGpuName(const Body: String; var Backend: String): Boolean;
begin
  Result := False;
  Backend := '';
  if StringContains(Body, 'NVIDIA') or StringContains(Body, 'GeForce') or
     StringContains(Body, 'Quadro') or StringContains(Body, 'Tesla') or
     StringContains(Body, 'RTX') or StringContains(Body, 'GTX') then begin
    Backend := 'nvidia';
    Result := True;
  end else if StringContains(Body, 'AMD') or StringContains(Body, 'Radeon') or
              StringContains(Body, 'Ryzen') then begin
    Backend := 'amd';
    Result := True;
  end else if StringContains(Body, 'Intel') and
              (StringContains(Body, 'Arc') or StringContains(Body, 'Iris Xe') or
               StringContains(Body, 'Xe Graphics') or StringContains(Body, 'UHD Graphics 7')) then begin
    Backend := 'intel';
    Result := True;
  end;
end;

function DetectNvidiaViaSmi(var GpuName: String): Boolean;
var
  OutFile, CmdLine, Body: String;
  ExitCode: Integer;
begin
  Result := False;
  GpuName := '';
  OutFile := ExpandConstant('{tmp}') + BACKSLASH + 'rapack-nvidiasmi.txt';
  CmdLine := '/c nvidia-smi --query-gpu=name --format=csv,noheader > "' + OutFile + '" 2>nul';
  if not ExecToFile(ExpandConstant('{cmd}'), CmdLine, OutFile, ExitCode) then Exit;
  if ExitCode <> 0 then Exit;
  if not FileExists(OutFile) then Exit;
  Body := Trim(ReadTextFile(OutFile));
  if Body = '' then Exit;
  GpuName := Body;
  Result := True;
end;

function DetectGpuViaPowerShell(var Backend, GpuName: String): Boolean;
var
  OutFile, CmdLine, Body: String;
  ExitCode: Integer;
begin
  Result := False;
  Backend := '';
  GpuName := '';
  OutFile := ExpandConstant('{tmp}') + BACKSLASH + 'rapack-gpups.txt';
  // We invoke PowerShell through cmd.exe so the > redirection is handled by
  // the shell instead of having to nest quoting around -Command.
  CmdLine := '/c powershell.exe -NoProfile -ExecutionPolicy Bypass ' +
             '-Command "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"' +
             ' > "' + OutFile + '" 2>nul';
  if not ExecToFile(ExpandConstant('{cmd}'), CmdLine, OutFile, ExitCode) then Exit;
  if not FileExists(OutFile) then Exit;
  Body := ReadTextFile(OutFile);
  if Trim(Body) = '' then Exit;
  GpuName := Trim(Body);
  Result := ClassifyGpuName(Body, Backend);
end;

function DetectGpuViaWmic(var Backend, GpuName: String): Boolean;
var
  OutFile, CmdLine, Body: String;
  ExitCode: Integer;
begin
  Result := False;
  Backend := '';
  GpuName := '';
  OutFile := ExpandConstant('{tmp}') + BACKSLASH + 'rapack-gpu-wmic.txt';
  CmdLine := '/c wmic path Win32_VideoController get Name /value > "' + OutFile + '" 2>nul';
  if not ExecToFile(ExpandConstant('{cmd}'), CmdLine, OutFile, ExitCode) then Exit;
  if not FileExists(OutFile) then Exit;
  Body := ReadTextFile(OutFile);
  if Trim(Body) = '' then Exit;
  GpuName := Trim(Body);
  Result := ClassifyGpuName(Body, Backend);
end;

function HasVulkanRuntime(): Boolean;
begin
  // vulkan-1.dll is shipped by every modern GPU driver (NVIDIA / AMD Adrenalin
  // / Intel) and by the standalone Vulkan Runtime installer. Presence in
  // System32 means apps in this process bitness can dlopen Vulkan.
  Result := FileExists(ExpandConstant('{sys}') + BACKSLASH + 'vulkan-1.dll');
end;

function DetectGpuSmart(var Backend, GpuName: String): Boolean;
var
  TmpName: String;
begin
  Result := False;
  Backend := '';
  GpuName := '';

  // 1. nvidia-smi: highest-confidence signal (driver loaded AND working).
  if DetectNvidiaViaSmi(TmpName) then begin
    Backend := 'nvidia';
    GpuName := TmpName + '  (nvidia-smi)';
    Result := True;
    Exit;
  end;

  // 2. PowerShell CIM (modern, replaces wmic on Win11 24H2+).
  if DetectGpuViaPowerShell(Backend, GpuName) then begin
    Result := True;
    Exit;
  end;

  // 3. wmic (legacy, may be absent on newer Windows).
  if DetectGpuViaWmic(Backend, GpuName) then begin
    Result := True;
    Exit;
  end;
end;

function RecommendVariantFromBackend(const Backend: String): String;
begin
  if Backend = 'nvidia' then
    Result := 'cuda'
  else if (Backend = 'amd') or (Backend = 'intel') then begin
    // Only recommend Vulkan if the runtime is actually present on disk;
    // otherwise the install would be inert until the user installs a Vulkan
    // ICD. Falling back to CPU is safer.
    if HasVulkanRuntime() then
      Result := 'vulkan'
    else
      Result := 'cpu';
  end else
    Result := 'cpu';
end;

{ --------------------------------------------------------------------------
  Existing-install detection: parse `llama-server --version` output.
  -------------------------------------------------------------------------- }

function DetectExistingVariant(const InstallDir: String; var Tag, ServerPath: String): Boolean;
var
  Candidate, OutFile, CmdLine, Body: String;
  ResultCode: Integer;
begin
  Result := False;
  Tag := '';
  ServerPath := '';

  Candidate := InstallDir + BACKSLASH + 'tools' + BACKSLASH + 'llama-server.exe';
  if not FileExists(Candidate) then Exit;
  ServerPath := Candidate;

  OutFile := ExpandConstant('{tmp}') + BACKSLASH + 'rapack-llamaver.txt';
  if FileExists(OutFile) then DeleteFile(OutFile);

  // Quote the executable path because the install dir may contain spaces.
  // The cmd.exe wrapper redirects stdout+stderr to OutFile for parsing.
  CmdLine := '/c "" "' + Candidate + '" --version > "' + OutFile + '" 2>&1';
  if Exec(ExpandConstant('{cmd}'), CmdLine, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then begin
    Body := ReadTextFile(OutFile);
    if StringContains(Body, 'CUDA')         then Tag := 'cuda'
    else if StringContains(Body, 'Vulkan')  then Tag := 'vulkan'
    else Tag := 'cpu';
    Result := True;
  end;
end;

{ --------------------------------------------------------------------------
  Custom wizard page.
  -------------------------------------------------------------------------- }

procedure CreateVariantPage();
var
  HwOk:           Boolean;
  Recommendation: String;
  Y:              Integer;
begin
  VariantPage := CreateCustomPage(
    wpSelectComponents,
    'Choose llama.cpp variant',
    'Pick the local inference backend that matches your hardware.');

  HwOk := DetectGpuSmart(DetectedBackend, DetectedGpuName);
  Recommendation := RecommendVariantFromBackend(DetectedBackend);

  HwInfoLabel := TNewStaticText.Create(VariantPage);
  HwInfoLabel.Parent := VariantPage.Surface;
  HwInfoLabel.Top := 0;
  HwInfoLabel.Left := 0;
  HwInfoLabel.AutoSize := True;
  HwInfoLabel.WordWrap := True;
  HwInfoLabel.Width := VariantPage.SurfaceWidth;
  if HwOk and (DetectedBackend <> '') then
    HwInfoLabel.Caption := 'Detected GPU (' + UpperCase(DetectedBackend) + '): ' + DetectedGpuName
  else
    HwInfoLabel.Caption := 'No GPU detected (or detection failed); CPU recommended.';

  RecommendLabel := TNewStaticText.Create(VariantPage);
  RecommendLabel.Parent := VariantPage.Surface;
  RecommendLabel.Top := HwInfoLabel.Top + HwInfoLabel.Height + 8;
  RecommendLabel.Left := 0;
  RecommendLabel.AutoSize := True;
  RecommendLabel.WordWrap := True;
  RecommendLabel.Width := VariantPage.SurfaceWidth;
  if (Recommendation = 'cpu') and ((DetectedBackend = 'amd') or (DetectedBackend = 'intel')) then
    RecommendLabel.Caption := 'Recommended: cpu' + #13#10 +
      '(GPU detected but vulkan-1.dll missing in System32; install GPU drivers' + #13#10 +
      ' or Vulkan Runtime then reinstall to enable Vulkan acceleration)'
  else
    RecommendLabel.Caption := 'Recommended: ' + Recommendation +
      '  (you can override below; "Skip" leaves no llama-server installed)';

  Y := RecommendLabel.Top + RecommendLabel.Height + 16;

  RadioCPU := TNewRadioButton.Create(VariantPage);
  RadioCPU.Parent := VariantPage.Surface;
  RadioCPU.Top := Y; RadioCPU.Left := 0;
  RadioCPU.Width := VariantPage.SurfaceWidth;
  RadioCPU.Caption := 'CPU AVX2 (~10 MB) - universal x86_64, slower for large models';
  Y := Y + RadioCPU.Height + 4;

  RadioCUDA := TNewRadioButton.Create(VariantPage);
  RadioCUDA.Parent := VariantPage.Surface;
  RadioCUDA.Top := Y; RadioCUDA.Left := 0;
  RadioCUDA.Width := VariantPage.SurfaceWidth;
  RadioCUDA.Caption := 'CUDA 12.x (~150 MB) - NVIDIA GPUs (requires CUDA 12+ driver)';
#if IncludeCuda == "0"
  RadioCUDA.Enabled := False;
  RadioCUDA.Caption := RadioCUDA.Caption + '   [not bundled in this Setup]';
#endif
  Y := Y + RadioCUDA.Height + 4;

  RadioVulkan := TNewRadioButton.Create(VariantPage);
  RadioVulkan.Parent := VariantPage.Surface;
  RadioVulkan.Top := Y; RadioVulkan.Left := 0;
  RadioVulkan.Width := VariantPage.SurfaceWidth;
  RadioVulkan.Caption := 'Vulkan (~30 MB) - NVIDIA/AMD/Intel via Vulkan driver';
#if IncludeVulkan == "0"
  RadioVulkan.Enabled := False;
  RadioVulkan.Caption := RadioVulkan.Caption + '   [not bundled in this Setup]';
#endif
  Y := Y + RadioVulkan.Height + 4;

  RadioSkip := TNewRadioButton.Create(VariantPage);
  RadioSkip.Parent := VariantPage.Surface;
  RadioSkip.Top := Y; RadioSkip.Left := 0;
  RadioSkip.Width := VariantPage.SurfaceWidth;
  RadioSkip.Caption := 'Skip - install no llama-server now (re-run Setup or point Server settings at an external binary later)';
  Y := Y + RadioSkip.Height + 4;

  // RadioKeep is created disabled with a placeholder caption. The actual
  // existing-install detection is deferred to RefreshVariantPageOnEnter
  // (called from CurPageChanged) because Inno Setup initializes {app} only
  // AFTER the user passes wpSelectDir; calling ExpandConstant('{app}') here
  // (during InitializeWizard) raises "Internal error: An attempt was made
  // to expand the 'app' constant before it was initialized."
  RadioKeep := TNewRadioButton.Create(VariantPage);
  RadioKeep.Parent := VariantPage.Surface;
  RadioKeep.Top := Y; RadioKeep.Left := 0;
  RadioKeep.Width := VariantPage.SurfaceWidth;
  RadioKeep.Caption := 'Keep existing (no prior install detected)';
  RadioKeep.Enabled := False;

  // The initial radio default selection is intentionally NOT set here.
  // Setting TNewRadioButton.Checked during InitializeWizard / CreateCustomPage
  // is unreliable in Inno 6 (the wizard's own auto-grouping pass on first
  // page-show appears to clear ad-hoc checks). We assign the default later
  // from RefreshVariantPageOnEnter (which fires from CurPageChanged at
  // page-show time), gated by VariantPageInitialized so the user's manual
  // pick is preserved if they navigate Back / Next.
end;

procedure RefreshVariantPageOnEnter();
var
  CurInstallDir:  String;
  ExistingExists: Boolean;
  Recommendation: String;
begin
  // Safe to ExpandConstant('{app}') here: this runs from CurPageChanged
  // when the user enters the variant page, which is after wpSelectDir.
  CurInstallDir  := ExpandConstant('{app}');
  ExistingExists := DetectExistingVariant(CurInstallDir, KeepExistingTag, KeepExistingPath);
  if ExistingExists then begin
    RadioKeep.Caption := 'Keep existing (' + KeepExistingTag + ' detected)';
    RadioKeep.Enabled := True;
  end else begin
    RadioKeep.Caption := 'Keep existing (no prior install detected)';
    RadioKeep.Enabled := False;
  end;

  // First time the user enters the page: assign the default selection.
  // Subsequent entries (Back -> Next) preserve whatever the user picked.
  //
  // The previous version always preselected "Keep existing" whenever a
  // prior install was detected, ignoring the hardware recommendation.
  // That trapped users who installed CPU once for testing on a CUDA
  // box: every subsequent reinstall defaulted to "Keep existing (cpu
  // detected)" even though the GPU clearly supported CUDA. Now we
  // preselect the recommended variant whenever it differs from the
  // existing one, and only fall back to "Keep existing" when the
  // existing variant already matches the recommendation (i.e. nothing
  // to upgrade) or when the recommendation is CPU and there is
  // already something on disk.
  if not VariantPageInitialized then begin
    Recommendation := RecommendVariantFromBackend(DetectedBackend);
    if ExistingExists and (KeepExistingTag = Recommendation) then
      RadioKeep.Checked := True
    else if (Recommendation = 'cuda') and RadioCUDA.Enabled then
      RadioCUDA.Checked := True
    else if (Recommendation = 'vulkan') and RadioVulkan.Enabled then
      RadioVulkan.Checked := True
    else if ExistingExists then
      RadioKeep.Checked := True
    else
      RadioCPU.Checked := True;
    VariantPageInitialized := True;
  end;
end;

function ResolveSelectedVariant(): String;
begin
  if RadioKeep.Checked        then Result := 'keep'
  else if RadioCUDA.Checked   then Result := 'cuda'
  else if RadioVulkan.Checked then Result := 'vulkan'
  else if RadioSkip.Checked   then Result := 'skip'
  else                              Result := 'cpu';
end;

{ --------------------------------------------------------------------------
  Component selection logic.
  -------------------------------------------------------------------------- }

procedure ApplyComponentSelection();
var
  ComponentsStr: String;
begin
  ComponentsStr := 'core,cpu';
  if SelectedVariant = 'cuda'   then ComponentsStr := ComponentsStr + ',cuda';
  if SelectedVariant = 'vulkan' then ComponentsStr := ComponentsStr + ',vulkan';
  WizardSelectComponents(ComponentsStr);
end;

function ShouldCopyVariant(const Variant: String): Boolean;
// [Files] Check: ShouldCopyVariant('<v>'). Only one variant gets copied
// into the install dir's tools\. "keep" and "skip" both leave tools\
// untouched.
begin
  if (SelectedVariant = 'keep') or (SelectedVariant = 'skip') then
    Result := False
  else
    Result := (Variant = SelectedVariant);
end;

{ --------------------------------------------------------------------------
  Backup helpers.
  -------------------------------------------------------------------------- }

procedure BackupExistingLlamaServer();
var
  Src, Dst: String;
begin
  Src := ExpandConstant('{app}') + BACKSLASH + 'tools' + BACKSLASH + 'llama-server.exe';
  Dst := Src + '.bak';
  if FileExists(Src) then begin
    if FileExists(Dst) then DeleteFile(Dst);
    FileCopy(Src, Dst, False);
  end;
end;

{ --------------------------------------------------------------------------
  Sentinel + server.yml seeding (plan §2.4.1.bis).
  -------------------------------------------------------------------------- }

function GpuLayersForVariant(const Variant: String): Integer;
begin
  if (Variant = 'cuda') or (Variant = 'vulkan') then Result := 99 else Result := 0;
end;

function FlashAttnForVariant(const Variant: String): String;
begin
  if Variant = 'cuda' then Result := 'true' else Result := 'false';
end;

procedure WriteInstallerSentinel(const Variant, LlamaPath: String; NGpuLayers: Integer);
var
  ConfigDir, SentinelPath, Body, RawGpu: String;
begin
  ConfigDir := ExpandConstant('{userappdata}') + BACKSLASH + 'report-anonymizer';
  ForceDirectories(ConfigDir);
  SentinelPath := ConfigDir + BACKSLASH + '.installer_choice.json';

  RawGpu := Copy(DetectedGpuName, 1, 256);

  Body :=
    '{' + #13#10 +
    '  "schema_version": 1,' + #13#10 +
    '  "installed_at": "' + GetUtcIsoNow() + '",' + #13#10 +
    '  "installer_version": "{#MyAppVersion}",' + #13#10 +
    '  "variant": "' + JsonEscape(Variant) + '",' + #13#10 +
    '  "llama_path": "' + JsonEscape(LlamaPath) + '",' + #13#10 +
    '  "n_gpu_layers": ' + IntToStr(NGpuLayers) + ',' + #13#10 +
    '  "hardware_detected": {' + #13#10 +
    '    "primary_gpu_backend": "' + JsonEscape(DetectedBackend) + '",' + #13#10 +
    '    "gpu_name_raw": "' + JsonEscape(RawGpu) + '"' + #13#10 +
    '  }' + #13#10 +
    '}' + #13#10;

  WriteTextFileUtf8(SentinelPath, Body);
end;

procedure WriteServerYamlSeed(const Variant, LlamaPath: String);
var
  ConfigDir, YamlPath, Body, Existing: String;
  GpuLayers: Integer;
  FlashAttn: String;
begin
  ConfigDir := ExpandConstant('{userappdata}') + BACKSLASH + 'report-anonymizer';
  ForceDirectories(ConfigDir);
  YamlPath := ConfigDir + BACKSLASH + 'server.yml';

  GpuLayers := GpuLayersForVariant(Variant);
  FlashAttn := FlashAttnForVariant(Variant);

  (*
    Write the installer-derived overrides directly on top of the
    builtin "default" preset. Earlier versions of Setup created a
    separate "installer-default" entry that extended default, which
    showed up as a second row in the preset wizard ("default" plus
    "installer-default" for what is the same model). The deep-merge
    in ``server_profile.load_profiles`` collapses any user-scope
    "default" override onto the canonical builtin, so we keep the
    profile list to a single "default" row no matter the variant.
  *)
  Body :=
    '# Auto-generated by Report Anonymizer Windows installer (variant=' + Variant + ').' + #13#10 +
    '# Edit freely; the installer overrides only the deployment-mode,' + #13#10 +
    '# binary path and GPU-layer fields of the canonical "default"' + #13#10 +
    '# preset. Other profiles you add by hand are preserved.' + #13#10 +
    'version: 1' + #13#10 +
    'profiles:' + #13#10 +
    '  - name: default' + #13#10 +
    '    deployment_mode: local_binary' + #13#10 +
    '    binary: "' + YamlEscapePath(LlamaPath) + '"' + #13#10 +
    '    n_gpu_layers: ' + IntToStr(GpuLayers) + #13#10 +
    '    flash_attn: ' + FlashAttn + #13#10;

  { Plan §M7: if a server.yml already exists and was hand-edited (no
    installer header marker), do NOT overwrite. }
  Existing := ReadTextFile(YamlPath);
  if (Existing <> '') and (Pos('Auto-generated by Report Anonymizer Windows installer', Existing) = 0) then begin
    Exit;
  end;
  WriteTextFileUtf8(YamlPath, Body);
end;

{ --------------------------------------------------------------------------
  Post-install smoke test.
  -------------------------------------------------------------------------- }

function RunLlamaVersionSmokeTest(): Boolean;
var
  Exe: String;
  ResultCode: Integer;
begin
  Result := False;
  Exe := ExpandConstant('{app}') + BACKSLASH + 'tools' + BACKSLASH + 'llama-server.exe';
  if not FileExists(Exe) then Exit;
  if Exec(Exe, '--version', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := (ResultCode = 0);
end;

procedure HandleSmokeTestFailure();
var
  Resp: Integer;
begin
  if WizardSilent then
    Resp := IDYES
  else
    Resp := MsgBox(
      'The selected llama-server variant (' + SelectedVariant + ') is not executable on this machine.' + #13#10 +
      '(driver / runtime missing?)' + #13#10#13#10 +
      'Install the CPU AVX2 fallback variant instead?',
      mbConfirmation, MB_YESNO);

  if Resp <> IDYES then Exit;

  if not DirExists(ExpandConstant('{app}') + BACKSLASH + 'llama-variants' + BACKSLASH + 'cpu') then begin
    MsgBox('CPU fallback binaries missing from this Setup. Reinstall and pick CPU.',
           mbError, MB_OK);
    Exit;
  end;

  CopyVariantToTools('cpu');
  SelectedVariant := 'cpu';
end;

{ --------------------------------------------------------------------------
  Uninstall.
  -------------------------------------------------------------------------- }

(*
  Sanity check before force-deleting the install root: the directory
  must contain at least one of our own sentinels (the launcher exe,
  the llama-variants tree, or the embedded Python tree). Without
  this, a misconfigured uninstall could nuke an unrelated folder if
  the user manually relocated bits. Delphi-style { ... } comments
  cannot be used here because Inno's preprocessor would treat the
  literal "{app}" inside them as a constant reference and the parser
  would close the comment prematurely.
*)
function _AppFolderLooksLikeOurs(const AppDir: String): Boolean;
begin
  Result :=
    FileExists(AppDir + BACKSLASH + 'launcher' + BACKSLASH + '{#MyAppExeName}') or
    DirExists (AppDir + BACKSLASH + 'llama-variants')                            or
    DirExists (AppDir + BACKSLASH + 'python');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Resp: Integer;
  AppData, LocalAppData, AppDir: String;
  RemoveUserData: Boolean;
begin
  if CurUninstallStep <> usPostUninstall then Exit;

  AppData      := ExpandConstant('{userappdata}')  + BACKSLASH + 'report-anonymizer';
  LocalAppData := ExpandConstant('{localappdata}') + BACKSLASH + 'report-anonymizer';
  AppDir       := ExpandConstant('{app}');

  (* Default: keep user data. Silent uninstalls (/VERYSILENT) skip the
     prompt and therefore keep settings + downloaded models on disk,
     which matches the conservative behaviour every other "uninstall"
     dialog on Windows defaults to. *)
  RemoveUserData := False;
  if DirExists(AppData) or DirExists(LocalAppData) then begin
    Resp := MsgBox(
      'Remove user data too?' + #13#10#13#10 +
      'Choose:' + #13#10 +
      '  - Yes -> also delete settings + downloaded models:' + #13#10 +
      '      %APPDATA%' + BACKSLASH + 'report-anonymizer  (server.yml, presets)' + #13#10 +
      '      %LOCALAPPDATA%' + BACKSLASH + 'report-anonymizer  (models, cache)' + #13#10 +
      '  - No  -> keep them so a future reinstall can reuse the' + #13#10 +
      '      models without re-downloading. The install folder is' + #13#10 +
      '      removed regardless of this choice.' + #13#10#13#10 +
      'Default (Esc / outside-click) is "No": user data stays.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2);

    if Resp = IDYES then RemoveUserData := True;
  end;

  if RemoveUserData then begin
    if DirExists(AppData)      then DelTree(AppData,      True, True, True);
    if DirExists(LocalAppData) then DelTree(LocalAppData, True, True, True);
  end;

  (* Always remove the install root once the unisntaller reaches this
     point. Inno's own bookkeeping leaves stray bytecode (.pyc and
     __pycache__ outside the two paths listed in [UninstallDelete]) on
     the next install we would otherwise hit the "Folder Exists"
     prompt. The DelTree is gated by _AppFolderLooksLikeOurs so we
     never touch a directory that doesn't carry one of our sentinels. *)
  if DirExists(AppDir) and _AppFolderLooksLikeOurs(AppDir) then begin
    DelTree(AppDir, True, True, True);
  end;
end;

{ --------------------------------------------------------------------------
  Event handlers.
  -------------------------------------------------------------------------- }

function InitializeSetup(): Boolean;
begin
  SelectedVariant        := '';
  DetectedBackend        := '';
  DetectedGpuName        := '';
  KeepExistingPath       := '';
  KeepExistingTag        := '';
  VariantPageInitialized := False;
  Result := True;
end;

procedure InitializeWizard();
begin
  CreateVariantPage();
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  { Hide the standard components page: our custom page drives selection. }
  if PageID = wpSelectComponents then Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (VariantPage <> nil) and (CurPageID = VariantPage.ID) then begin
    SelectedVariant := ResolveSelectedVariant();
    ApplyComponentSelection();
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  // Refresh the variant page each time the user enters it so the existing-
  // install probe reflects the current install dir (which may change if the
  // user goes Back to wpSelectDir and edits it).
  if (VariantPage <> nil) and (CurPageID = VariantPage.ID) then
    RefreshVariantPageOnEnter();
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  LlamaPath: String;
  Variant:   String;
  NLayers:   Integer;
  Ok:        Boolean;
begin
  if CurStep = ssInstall then begin
    if WizardSilent and (SelectedVariant = '') then begin
      SelectedVariant := GetSilentVariant();
      ApplyComponentSelection();
    end;
    if SelectedVariant <> 'keep' then
      BackupExistingLlamaServer();
  end;

  if CurStep = ssPostInstall then begin
    Variant := SelectedVariant;
    LlamaPath := ExpandConstant('{app}') + BACKSLASH + 'tools' + BACKSLASH + 'llama-server.exe';

    if Variant = 'skip' then begin
      { Plan §M6: no sentinel, no seed. }
      Exit;
    end;

    if Variant = 'keep' then begin
      if KeepExistingPath <> '' then LlamaPath := KeepExistingPath;
      if KeepExistingTag  <> '' then Variant := KeepExistingTag
      else                            Variant := 'cpu';
    end;

    Ok := RunLlamaVersionSmokeTest();
    if not Ok then begin
      HandleSmokeTestFailure();   { mutates SelectedVariant on success }
      Variant := SelectedVariant;
      LlamaPath := ExpandConstant('{app}') + BACKSLASH + 'tools' + BACKSLASH + 'llama-server.exe';
      Ok := RunLlamaVersionSmokeTest();
      if not Ok then begin
        if not WizardSilent then
          MsgBox('llama-server is not executable on this machine. The app will start, but inference must be configured manually.',
                 mbError, MB_OK);
        Exit;
      end;
    end;

    NLayers := GpuLayersForVariant(Variant);
    WriteInstallerSentinel(Variant, LlamaPath, NLayers);
    WriteServerYamlSeed(Variant, LlamaPath);
  end;
end;
