@echo off
chcp 936 >nul 2>nul
rem MoneyTrail · 资金链路核查台 —— 双击即可启动
rem 1) 切到脚本所在目录，避免从别处双击时找不到相对路径
rem 2) 缺 moneytrail.db 或 data\orders.csv 时先跑 gen_data.py 生成演示数据
rem 3) 启动 app.py，并提示浏览器打开 http://127.0.0.1:8000
setlocal
cd /d "%~dp0"

title MoneyTrail 资金链路核查台

rem ---- 定位 Python：PATH 里的 python.exe 可能是应用商店的占位符，
rem      所以候选解释器一律先跑一次 --version 实测，能跑才采用
set "PY="
call :probe py -3
if not defined PY call :probe python
for %%V in (314 313 312 311) do (
    if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
    )
)
if not defined PY goto :no_python

rem ---- 缺数据就先生成（gen_data.py 固定随机种子，结果可复现）
if not exist "moneytrail.db" goto :generate
if not exist "data\orders.csv" goto :generate
echo [1/2] 演示数据已存在，跳过生成
goto :serve

:generate
echo [1/2] 正在生成演示数据（首次启动只需一次，约 1 秒）……
%PY% gen_data.py
if errorlevel 1 (
    echo.
    echo [错误] 演示数据生成失败，请把上面的报错信息一并反馈。
    pause
    exit /b 1
)
echo.

:serve
echo [2/2] 正在启动服务，浏览器请打开： http://127.0.0.1:8000
echo       按 Ctrl+C 停止服务。
echo.
%PY% app.py
if errorlevel 1 (
    echo.
    echo [错误] 服务异常退出。若提示端口被占用，可改用： %PY% app.py --port 8001
    pause
    exit /b 1
)
echo.
echo 服务已停止。
pause
exit /b 0

:no_python
echo [错误] 没有找到可用的 Python。
echo        请安装 Python 3.10 或更高版本，安装时勾选 "Add python.exe to PATH"。
echo.
pause
exit /b 1

:probe
rem 试跑一次 --version；成功（errorlevel 0）才把它记为解释器
%* --version >nul 2>nul
if not errorlevel 1 set "PY=%*"
goto :eof
