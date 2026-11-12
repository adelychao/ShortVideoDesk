@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The local workbench Python environment is missing.
  echo Follow the setup instructions in README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info < (3, 11))"
if errorlevel 1 (
  echo Python 3.11 or newer is required in .venv.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import fastapi, keyring, multipart, uvicorn, yt_dlp" >nul 2>nul
if errorlevel 1 (
  echo One or more Python dependencies are missing.
  echo Run: uv pip install --python .venv\Scripts\python.exe -r requirements-poc.txt
  pause
  exit /b 1
)
where node >nul 2>nul
if errorlevel 1 (
  echo Node.js is required to run the Vite frontend.
  echo Install Node.js and reopen this terminal.
  pause
  exit /b 1
)
where npm >nul 2>nul
if errorlevel 1 (
  echo npm is required to install the Vite frontend dependencies.
  echo Install Node.js with npm and reopen this terminal.
  pause
  exit /b 1
)
if not exist "frontend\node_modules\vite\bin\vite.js" (
  echo Frontend dependencies are missing.
  echo Run: npm --prefix frontend ci
  pause
  exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 echo Warning: FFmpeg was not found. Audio extraction and some downloads may be unavailable.
where ffprobe >nul 2>nul
if errorlevel 1 echo Warning: FFprobe was not found. Media inspection will be limited.
set PYTHONUTF8=1
set OMD_FRONTEND_DEV=1
".venv\Scripts\python.exe" launcher.py --port 8767 --asgi-app anonymous_poc:app --frontend-dev %*
if errorlevel 1 pause
