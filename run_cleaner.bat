@echo off
title Antigravity Disk Cleaner Launcher
echo ===================================================
echo   Starting Antigravity Disk Cleaner Backend...
echo ===================================================
cd /d "%~dp0"

:: Start the Python backend inside the src/ folder
start "" /B python src\cleaner_backend.py

echo Waiting for API server to initialize on port 8342...
timeout /t 2 /nobreak >nul

echo Opening browser at http://localhost:8342 ...
start http://localhost:8342

echo ===================================================
echo   System running. Close this command prompt to stop.
echo ===================================================
pause
