@echo off
chcp 65001 >nul
rem Pixiv 图片查看器 启动脚本（Windows）
rem   run_web.bat            正常启动（无控制台窗口）
rem   run_web.bat --console  带控制台启动，用于查看报错 / 日志
rem 指定 Python 解释器：设置环境变量 PIXIV_VIEWER_PYTHON 为 python.exe 的完整路径，
rem   或者在本文件夹里放一个 python-path.txt，里面只写这一行路径（这个文件不进版本库）
setlocal
cd /d "%~dp0"
if not defined PIXIV_VIEWER_PYTHON if exist "%~dp0python-path.txt" set /p PIXIV_VIEWER_PYTHON=<"%~dp0python-path.txt"

rem 依次尝试这些解释器，选第一个装了 pywebview 的
set "PY="
for %%P in ("%CD%\.venv\Scripts\python.exe" "%PIXIV_VIEWER_PYTHON%" "%USERPROFILE%\miniconda3\python.exe" "%USERPROFILE%\anaconda3\python.exe" "python.exe") do (
    if not defined PY (
        "%%~P" -c "import webview, PIL" >nul 2>nul && set "PY=%%~P"
    )
)

if not defined PY (
    echo [错误] 没有找到已安装 pywebview 的 Python 解释器。
    echo.
    echo 请先安装依赖:  python -m pip install -r requirements.txt
    echo 或者设置环境变量 PIXIV_VIEWER_PYTHON 指向合适的 python.exe
    echo.
    pause
    exit /b 1
)

if /i "%~1"=="--console" (
    echo 使用解释器: %PY%
    "%PY%" web_main.py
    if errorlevel 1 (
        echo.
        echo [程序异常退出，退出码 %errorlevel%]
        pause
    )
    exit /b %errorlevel%
)

rem 无控制台模式优先使用 pythonw.exe
set "PYW=%PY:python.exe=pythonw.exe%"
if exist "%PYW%" (
    start "" "%PYW%" web_main.py
) else (
    start "" "%PY%" web_main.py
)
exit /b 0
