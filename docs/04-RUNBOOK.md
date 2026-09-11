# Disk CleanUp v2.0 — Operations Runbook

This runbook documents setup, testing, packaging, release verification, troubleshooting, and rollback procedures for maintainers and operators of Disk CleanUp v2.0.

---

## 1. System Requirements & Prerequisites

### Target Machine (End Users)
- **Operating System**: Windows 10 (version 21H2 / build 19044 or later) or Windows 11 (64-bit).
- **Runtime**: Microsoft Edge WebView2 Runtime (pre-installed on Windows 11 and modern Windows 10 updates).
- **Permissions**: Standard user account for scanning and general cache cleaning; Administrator privileges required for VSS Shadow Storage resizing and WSL2 VHDX disk compaction.

### Developer & Build Environment
- **Python**: CPython 3.12.x (pinned to 3.12 due to `pythonnet==3.1.0` binary dependency; Python 3.14 is not supported).
- **Package Manager**: [uv](https://github.com/astral-sh/uv) (fast Python package installer & resolver).
- **Installer Compiler**: Inno Setup 6.7+ (installable via `winget install JRSoftware.InnoSetup`).

---

## 2. Daily Development Workflow

### Setup Virtual Environment
```powershell
# In project root: E:\PROJECT\antigravity-disk-cleaner
uv sync
```

### Running Locally from Source
```powershell
# Start the desktop application
uv run python -m adc

# Run in debug mode (opens Chromium Developer Tools on F12)
uv run python -m adc --debug

# Run self-check diagnostics without opening GUI
uv run python -m adc --self-check
```

### Quality Gate Checks (Pre-Commit)
Always verify all three quality gates before submitting changes:
```powershell
# 1. Full automated test suite (959 tests)
uv run pytest -q --tb=short

# 2. Fast linter & import order checks
uv run ruff check

# 3. Static type analysis across engine and shell boundary
uv run mypy src/adc/engine src/adc/shell
```

---

## 3. Production Build & Release Procedure

### Automated Build Pipeline
Run the unified build script from PowerShell:
```powershell
pwsh -File build.ps1
```

The script executes the following four-stage sequence:
1. **Environment Check**: Confirms active Python is 3.12.x.
2. **Quality Gate**: Executes `pytest`. If any test fails, the build terminates immediately.
3. **PyInstaller Compilation**: Builds the onedir standalone bundle (`dist/DiskCleanUp/`), embeds the manifest (`asInvoker`, High-DPI), and validates `--self-check` execution.
4. **Inno Setup Compilation**: Compiles `build/installer.iss` using `ISCC.exe`, outputs `dist/DiskCleanUp-Setup-2.0.0-x64.exe`, and computes SHA-256 checksums into `dist/SHA256SUMS.txt`.

### Build Verification Checklist
- [ ] `dist/DiskCleanUp/DiskCleanUp.exe` runs without displaying a black console window.
- [ ] Bundle footprint is under 60 MB (currently ~31.5 MB).
- [ ] `dist/DiskCleanUp-Setup-2.0.0-x64.exe` is under 20 MB (currently ~12.7 MB).
- [ ] SHA-256 checksum in `dist/SHA256SUMS.txt` matches `Get-FileHash dist/DiskCleanUp-Setup-2.0.0-x64.exe`.

---

## 4. Diagnostics & Troubleshooting

### Diagnostic Logs
Application logs are automatically written to:
```text
%LOCALAPPDATA%\DiskCleanUp\logs\adc-YYYYMMDD.log
```
Logs contain:
- Process startup parameters and elevation status.
- WebView2 initialization and version checks.
- Deleted items with strategy, size, and duration.
- Guard refusals and path fence exceptions.

### Known Edge Cases & Remediation

#### 1. Windows SmartScreen "Windows protected your PC"
- **Cause**: The executable is built without an expensive commercial code-signing certificate (as documented in `02-SPEC.md` §8.1).
- **Remediation**: Click **More info** -> **Run anyway**. For internal/team deployments, the SHA256 checksum in `dist/SHA256SUMS.txt` serves as proof of binary integrity.

#### 2. WebView2 Runtime Missing
- **Symptoms**: Startup fails with `WebView2Loader.dll` error or dialog stating WebView2 is missing.
- **Remediation**: Download and run the Microsoft Edge WebView2 Evergreen Bootstrapper:
  [https://go.microsoft.com/fwlink/p/?LinkId=2124703](https://go.microsoft.com/fwlink/p/?LinkId=2124703)

#### 3. Access Denied on VSS / Docker Compaction
- **Symptoms**: Target rows for VSS or WSL2 show locked / disabled icon.
- **Remediation**: Click the **"Khởi động lại với quyền Administrator"** banner in the bottom-left sidebar. The app will smoothly transfer state and relaunch elevated via UAC without terminating running jobs unsafely.

---

## 5. Rollback Procedure

If a release build introduces an unexpected regression:
1. Keep the previous installer (`DiskCleanUp-Setup-1.x` or previous release) accessible in archive storage.
2. Running the earlier installer will perform an in-place downgrade; the fixed `AppId` (`{{A57E5779-1B6E-4CF6-896F-92DFEE26992F}}`) ensures files are cleanly replaced without creating duplicate Windows Add/Remove entries.
3. User settings and history stored in `%LOCALAPPDATA%\DiskCleanUp` are backwards-compatible and will not be destroyed during a rollback.

