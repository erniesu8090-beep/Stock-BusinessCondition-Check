@echo off
cd /d "%~dp0"
title Taiwan Stock Whale Risk Dashboard

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating Python virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create .venv. Please check Python installation.
        pause
        exit /b 1
    )
    echo [2/3] Installing requirements...
    ".venv\Scripts\pip.exe" install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies.
        pause
        exit /b 1
    )
)

echo [3/3] Starting Dashboard Server and syncing market data...
".venv\Scripts\python.exe" "src\server.py"
if errorlevel 1 (
    echo Server exited with error.
    pause
)
