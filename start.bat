@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not defined PORT set PORT=8000
if not defined DEMO_MODE set DEMO_MODE=1

echo ============================================
echo   ZeroStep 零步银行 - 本地演示
echo   打开浏览器访问 http://127.0.0.1:%PORT%/
echo   按 Ctrl+C 停止服务
echo ============================================
echo.

where python >nul 2>nul
rem 在后台等 2 秒再打开浏览器，避免服务还没监听就白屏（用 python 单行实现，避免 cmd 嵌套引号）
set OPEN_CMD=import time,webbrowser,sys;time.sleep(2);webbrowser.open(sys.argv[1])
if %errorlevel%==0 (
    start "" /b python -c "%OPEN_CMD%" http://127.0.0.1:%PORT%/
    python backend\server.py
    goto :eof
)
where py >nul 2>nul
if %errorlevel%==0 (
    start "" /b py -3 -c "%OPEN_CMD%" http://127.0.0.1:%PORT%/
    py -3 backend\server.py
    goto :eof
)
echo 未找到 Python，请先安装 Python 3.10 及以上版本：https://www.python.org/downloads/
pause
