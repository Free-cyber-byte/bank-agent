@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

where python >nul 2>nul
if %errorlevel%==0 (
    python scripts\stop_services.py %*
    pause
    goto :eof
)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 scripts\stop_services.py %*
    pause
    goto :eof
)
echo Python not found. Install Python 3.10+ first: https://www.python.org/downloads/
pause