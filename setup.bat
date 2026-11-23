@echo off
rem MVSync setup (Windows): creates .venv next to this file with everything the worker needs
rem (Python 3.12, numpy/scipy, ShazamIO, yt-dlp, ffmpeg, Deno). Nothing else to install.
rem If uv is not installed, it is downloaded into .tools\ . The TouchDesigner "Install" button
rem runs this same file (with MVSYNC_NO_PAUSE=1).
setlocal
cd /d "%~dp0"
set "ROOT=%cd%"
set "UV_PYTHON_INSTALL_DIR=%ROOT%\.tools\python"
set "UV="
for /f "delims=" %%i in ('where uv 2^>nul') do if not defined UV set "UV=%%i"
if not defined UV if exist "%ROOT%\.tools\uv\uv.exe" set "UV=%ROOT%\.tools\uv\uv.exe"
if not defined UV (
  echo [1/3] downloading uv
  powershell -NoProfile -ExecutionPolicy ByPass -Command "$env:UV_INSTALL_DIR='%ROOT%\.tools\uv'; $env:UV_NO_MODIFY_PATH='1'; irm https://astral.sh/uv/install.ps1 | iex" || goto :fail
  set "UV=%ROOT%\.tools\uv\uv.exe"
)
echo [2/3] Python 3.12 environment (.venv)
"%UV%" venv --allow-existing --python 3.12 .venv || goto :fail
echo [3/3] packages
"%UV%" pip install --python .venv\Scripts\python.exe -U -r worker\requirements.txt || goto :fail
.venv\Scripts\python.exe -c "import numpy, scipy, sounddevice, pythonosc, shazamio, yt_dlp, imageio_ffmpeg, deno; print('check ok')" || goto :fail
echo OK: %ROOT%\.venv
if not defined MVSYNC_NO_PAUSE pause
exit /b 0
:fail
echo Setup failed.
if not defined MVSYNC_NO_PAUSE pause
exit /b 1
