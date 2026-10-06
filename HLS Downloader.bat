@echo off
cd /d "%~dp0"
if exist "%~dp0dist\HLS Downloader\HLS Downloader.exe" (
  start "" "%~dp0dist\HLS Downloader\HLS Downloader.exe"
  exit /b 0
)
where pythonw >nul 2>&1
if %errorlevel%==0 (
  start "" pythonw "%~dp0hls_downloader.py"
  exit /b 0
)
python "%~dp0hls_downloader.py"
if errorlevel 1 pause
