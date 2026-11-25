@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Virtual environment not found at .venv\Scripts\python.exe
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m gemini_web2api --config config.json
if errorlevel 1 (
  echo.
  echo Server exited with an error.
  pause
)
