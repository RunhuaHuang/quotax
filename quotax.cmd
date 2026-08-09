@echo off
setlocal EnableDelayedExpansion
rem ==============================================================================
rem quotax — QuotaX 一键启动器（由 install.ps1 安装到 PATH）
rem
rem 用法：
rem   quotax          启动服务（已在运行则直接打开浏览器）
rem   quotax stop     停止后台服务
rem   quotax status   查看运行状态
rem ==============================================================================

set "PERM_DIR=%USERPROFILE%\QuotaX"
if defined QUOTAX_HOME set "PERM_DIR=%QUOTAX_HOME%"
set "PORT=8900"
if defined QUOTAX_PORT set "PORT=%QUOTAX_PORT%"
set "URL=http://127.0.0.1:%PORT%"

rem 判断端口是否在监听（用 powershell 探测）
call :is_running
set "RUNNING=%ERRORLEVEL%"

if "%~1"=="" goto :start
if /I "%~1"=="start" goto :start
if /I "%~1"=="stop" goto :stop
if /I "%~1"=="status" goto :status
echo 用法: quotax [start^|stop^|status] 1>&2
exit /b 2

:start
if "%RUNNING%"=="0" (
  echo QuotaX 已在运行 -^> 打开浏览器
  start "" "%URL%"
  exit /b 0
)
if not exist "%PERM_DIR%\app\main.py" (
  echo Error: 未找到 QuotaX 安装目录 %PERM_DIR% 1>&2
  echo        请先运行安装脚本，或设置 QUOTAX_HOME 指向安装目录。 1>&2
  exit /b 1
)
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>&1
if errorlevel 1 (
  echo Error: 未找到 uv。请先安装 uv (winget install astral-sh.uv^)。 1>&2
  exit /b 1
)
echo 启动 QuotaX（端口 %PORT%）...
rem 用后台 powershell 启动 uvicorn，无窗口，输出重定向到日志。
set "LOG_FILE=%PERM_DIR%\quotax.log"
start "" /B powershell -NoProfile -WindowStyle Hidden -Command ^
  "Set-Location '%PERM_DIR%'; & uv run uvicorn app.main:app --host 127.0.0.1 --port %PORT% *>&1 | Tee-Object -FilePath '%LOG_FILE%'"
rem 轮询等待就绪（最多 30 秒）
for /l %%i in (1,1,30) do (
  call :is_running
  if !ERRORLEVEL!==0 (
    echo  ✅
    start "" "%URL%"
    echo 地址: %URL%
    echo 日志: %LOG_FILE%
    exit /b 0
  )
  timeout /t 1 /nobreak >nul
)
echo ⚠️  服务仍在启动中，请稍后访问 %URL%
echo    日志: %LOG_FILE%
exit /b 0

:stop
if "%RUNNING%"=="0" (
  for /f "tokens=5" %%a in ('netstat -ano -p tcp ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
  )
  echo QuotaX 已停止
) else (
  echo QuotaX 未在运行
)
exit /b 0

:status
if "%RUNNING%"=="0" (
  echo ✅ QuotaX 运行中 -^> %URL%
) else (
  echo ❌ QuotaX 未运行（用 quotax 启动）
)
exit /b 0

rem :is_running — 返回 ERRORLEVEL 0=运行中, 1=未运行
:is_running
powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri '%URL%' -UseBasicParsing -TimeoutSec 1).StatusCode -eq 200 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
exit /b %ERRORLEVEL%
