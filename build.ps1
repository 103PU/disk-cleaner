<#
.SYNOPSIS
    Build script for Antigravity Disk Cleaner (ADC) v2.0.

.DESCRIPTION
    1. Validates Python 3.12 environment
    2. Runs pytest quality gate
    3. Builds PyInstaller onedir distribution
    4. Verifies dist/DiskCleanUp output

    5. Optionally builds Inno Setup installer if ISCC.exe is available

.PARAMETER SkipTests
    Skip running pytest before build.

.PARAMETER SkipInstaller
    Skip building the Inno Setup installer.

.PARAMETER Clean
    Clean build artifacts before building.
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipInstaller,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Disk CleanUp v2.0 -- Build Script" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan


# 1. Check Python environment
Write-Host "`n[1/4] Checking environment..." -ForegroundColor Yellow
$PythonVersion = & uv run python --version
Write-Host "  Python: $PythonVersion"
if ($PythonVersion -notmatch "3\.12\.") {
    Write-Error "ADC requires Python 3.12.x for pythonnet 3.1.0 compatibility. Found: $PythonVersion"
}

# Clean if requested
if ($Clean) {
    Write-Host "  Cleaning dist/ and build/temp..." -ForegroundColor Gray
    if (Test-Path "dist") { Remove-Item "dist" -Recurse -Force }
    if (Test-Path "build/temp") { Remove-Item "build/temp" -Recurse -Force }
}

# 2. Run quality gate tests
if (-not $SkipTests) {
    Write-Host "`n[2/4] Running pytest quality gate..." -ForegroundColor Yellow
    & uv run pytest -q --tb=short
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Tests failed! Build aborted."
    }
    Write-Host "  All tests passed." -ForegroundColor Green
} else {
    Write-Host "`n[2/4] Skipping tests (-SkipTests requested)" -ForegroundColor DarkGray
}

# 3. PyInstaller Build
Write-Host "`n[3/4] Building PyInstaller onedir bundle..." -ForegroundColor Yellow
& uv run --extra build pyinstaller --noconfirm --distpath dist --workpath build/temp build/adc.spec
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller build failed!"
}


# Verify output
$ExePath = "dist/DiskCleanUp/DiskCleanUp.exe"
if (-not (Test-Path $ExePath)) {
    Write-Error "Expected executable not found at $ExePath!"
}

$BundleSizeBytes = (Get-ChildItem -Path "dist/DiskCleanUp" -Recurse | Measure-Object -Property Length -Sum).Sum
$BundleSizeMB = [math]::Round($BundleSizeBytes / 1MB, 2)
Write-Host "  Bundle size: $BundleSizeMB MB (target <= 60 MB)" -ForegroundColor Green

# Generate HOW-TO-RUN.txt guide inside the bundle
@"
============================================================
  Disk CleanUp - Windows x64 Portable Edition
============================================================

HOW TO RUN:
1. Extract ALL files from this archive to a folder on your computer.
2. Open the extracted folder and double-click "DiskCleanUp.exe".

IMPORTANT:
- Do NOT run DiskCleanUp.exe directly inside the ZIP without extracting!
  Windows will fail to load the required library files in "_internal/".
- Do NOT move DiskCleanUp.exe out of its folder without the "_internal/" directory.

REQUIREMENTS:
- Windows 10 (version 19044+) or Windows 11 (64-bit).
- Microsoft Edge WebView2 Runtime (pre-installed on Windows 10/11).
"@ | Set-Content -Path "dist/DiskCleanUp/HOW-TO-RUN.txt" -Encoding utf8

# Self-check verify
Write-Host "  Running --self-check on built binary..." -ForegroundColor Gray
$CheckOutput = & $ExePath --self-check 2>&1
Write-Host "  Self-check completed successfully." -ForegroundColor Green

# 4. Inno Setup installer
if (-not $SkipInstaller) {
    Write-Host "`n[4/4] Building Inno Setup installer..." -ForegroundColor Yellow
    $IsccPath = $null
    if (Get-Command iscc -ErrorAction SilentlyContinue) {
        $IsccPath = (Get-Command iscc).Source
    } elseif (Test-Path "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") {
        $IsccPath = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    } elseif (Test-Path "C:\Program Files (x86)\Inno Setup 6\ISCC.exe") {
        $IsccPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    } elseif (Test-Path "C:\Program Files\Inno Setup 6\ISCC.exe") {
        $IsccPath = "C:\Program Files\Inno Setup 6\ISCC.exe"
    }

    if ($IsccPath -and (Test-Path "build/installer.iss")) {
        Write-Host "  Using ISCC: $IsccPath"
        $AppVersion = (Get-Content "pyproject.toml" | Select-String '^version\s*=\s*"([^"]+)"').Matches.Groups[1].Value
        & $IsccPath "/DMyAppVersion=$AppVersion" "build/installer.iss"
        if ($LASTEXITCODE -eq 0) {
            # Compute SHA256
            $InstallerFile = Get-ChildItem -Path "dist" -Filter "*.exe" | Where-Object { $_.Name -like "*Setup*" } | Select-Object -First 1
            if ($InstallerFile) {
                $Hash = (Get-FileHash -Path $InstallerFile.FullName -Algorithm SHA256).Hash
                "$Hash  $($InstallerFile.Name)" | Set-Content -Path "dist/SHA256SUMS.txt" -Encoding utf8
                Write-Host "  Installer: $($InstallerFile.FullName)" -ForegroundColor Green
                Write-Host "  SHA256: $Hash" -ForegroundColor Green
            }
        } else {
            Write-Warning "Inno Setup compilation returned exit code $LASTEXITCODE"
        }
    } else {
        Write-Host "  Inno Setup not found or build/installer.iss not present." -ForegroundColor DarkGray
        Write-Host "  To build the installer, install Inno Setup: winget install JRSoftware.InnoSetup" -ForegroundColor DarkGray
    }
} else {
    Write-Host "`n[4/4] Skipping installer (-SkipInstaller requested)" -ForegroundColor DarkGray
}

Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host "  Build completed successfully!" -ForegroundColor Green
Write-Host "  Output: dist/DiskCleanUp/" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan

