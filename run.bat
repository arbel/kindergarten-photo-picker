@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM --- Locate a usable Python ---
set PYTHON=
where py >nul 2>&1 && set PYTHON=py -3
if not defined PYTHON (
    where python >nul 2>&1 && set PYTHON=python
)

if not defined PYTHON (
    echo Python 3.11+ is required and was not found on this machine.
    echo.
    echo Attempting to install Python 3.13 with winget...
    winget install -e --id Python.Python.3.13 --scope user --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo.
        echo Automatic install failed. Please install Python manually from:
        echo   https://www.python.org/downloads/
        echo During install, tick "Add python.exe to PATH".
        echo Then close this window and double-click run.bat again.
        pause
        exit /b 1
    )
    echo.
    echo Python installed. Close this window and double-click run.bat again
    echo to finish setup and launch the app.
    pause
    exit /b 0
)

REM --- First-run venv + deps setup ---
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PYTHON% -m venv .venv || goto :error
    .venv\Scripts\python -m pip install --upgrade pip || goto :error
    echo Installing dependencies (approx 200 MB, one-time download)...
    .venv\Scripts\pip install -r requirements.txt || goto :error
)

.venv\Scripts\python -m src.main %*
exit /b %errorlevel%

:error
echo.
echo Setup failed. See message above.
pause
exit /b 1
