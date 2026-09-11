@echo off
setlocal EnableDelayedExpansion
title Disk CleanUp Launcher
cd /d "%~dp0"

echo ===================================================
echo   Disk CleanUp - Launcher
echo ===================================================

:: ---------------------------------------------------------------
:: P0: find a REAL CPython.
:: Bare `python` on Windows 10 often resolves to the Microsoft Store
:: alias stub in %LOCALAPPDATA%\Microsoft\WindowsApps. That stub opens
:: the Store instead of running the script, and it still shows up in
:: `where python`, so a path test alone is not enough.
:: We therefore PROBE each candidate: a real interpreter prints "3".
:: ---------------------------------------------------------------
set "PYEXE="
set "PYARGS="

call :probe "py" "-3"
if not defined PYEXE call :probe "python" ""
if not defined PYEXE call :probe "python3" ""

if not defined PYEXE (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if not defined PYEXE if exist "%%~D\python.exe" call :probe "%%~D\python.exe" ""
    )
)

if not defined PYEXE (
    echo [ERROR] No usable Python interpreter found.
    echo         Every candidate on PATH failed the version probe, which is what
    echo         the Microsoft Store alias stub does when Python is not installed.
    echo         Install a real interpreter:
    echo             winget install Python.Python.3.12
    goto :end
)

:: If the compiled standalone executable exists, launch it directly
if exist "%~dp0dist\DiskCleanUp\DiskCleanUp.exe" (
    echo Launching Disk CleanUp v2.0...
    start "" "%~dp0dist\DiskCleanUp\DiskCleanUp.exe" %*
    goto :end
)

:: Otherwise, launch via uv or python -m adc
where uv >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo Launching Disk CleanUp via uv...
    start "" uv run python -m adc %*
    goto :end
)

if defined PYEXE (
    echo Launching Disk CleanUp via Python...
    start "" "%PYEXE%" %PYARGS% -m adc %*
    goto :end
)
:end

endlocal
exit /b 0

:: ---------------------------------------------------------------
:: :probe <command> <extra-args>
:: Sets PYEXE/PYARGS only if the candidate really is CPython 3.
:: The Store alias stub answers nothing and exits non-zero, so it is
:: filtered out here rather than by matching on "\WindowsApps\".
:: ---------------------------------------------------------------
:probe
set "_CAND=%~1"
set "_CARG=%~2"
set "_PROBE=%TEMP%\adc_probe_%RANDOM%%RANDOM%.txt"
"%_CAND%" %_CARG% -c "import sys;print(sys.version_info[0])" >"%_PROBE%" 2>nul
set "_V="
if exist "%_PROBE%" for /f "usebackq delims=" %%V in ("%_PROBE%") do set "_V=%%V"
if exist "%_PROBE%" del /q "%_PROBE%" >nul 2>&1
if "%_V%"=="3" (
    set "PYEXE=%_CAND%"
    set "PYARGS=%_CARG%"
)
set "_CAND="
set "_CARG="
set "_PROBE="
set "_V="
exit /b 0
