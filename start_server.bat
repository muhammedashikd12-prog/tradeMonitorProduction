@echo off
setlocal

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

echo Starting Condor AI on http://0.0.0.0:8000
start "Condor AI" http://127.0.0.1:8000
%PYTHON% -m uvicorn app.main:app --host 0.0.0.0 --port 8000

endlocal