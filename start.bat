@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  .venv\Scripts\python.exe src\bootstrap.py %*
) else (
  where py >nul 2>nul
  if errorlevel 1 (python src\bootstrap.py %*) else (py -3 src\bootstrap.py %*)
)
if errorlevel 1 pause
