@echo off
rem Creates the worker's Python environment (.venv next to this file) for MVSync on Windows.
rem Needs: uv (winget install astral-sh.uv) or Python 3.12 (py launcher), plus ffmpeg and node.
setlocal
cd /d "%~dp0"
where uv >nul 2>nul
if %errorlevel%==0 (
  uv venv --python 3.12 .venv || goto :fail
  uv pip install --python .venv\Scripts\python.exe -r worker\requirements.txt || goto :fail
) else (
  py -3.12 -m venv .venv || (echo Python 3.12 is required. Install uv or Python 3.12. & goto :fail)
  .venv\Scripts\python.exe -m pip install -U pip || goto :fail
  .venv\Scripts\python.exe -m pip install -r worker\requirements.txt || goto :fail
)
where ffmpeg >nul 2>nul || echo WARNING: ffmpeg not found (winget install Gyan.FFmpeg)
where node >nul 2>nul || echo WARNING: node not found (winget install OpenJS.NodeJS.LTS)
echo OK: %cd%\.venv
pause
exit /b 0
:fail
echo Setup failed.
pause
exit /b 1
