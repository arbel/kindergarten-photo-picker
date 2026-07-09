@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv || goto :error
    .venv\Scripts\python -m pip install --upgrade pip || goto :error
    .venv\Scripts\pip install -r requirements.txt || goto :error
)

.venv\Scripts\python -m src.main %*
exit /b %errorlevel%

:error
echo.
echo Setup failed.
exit /b 1
