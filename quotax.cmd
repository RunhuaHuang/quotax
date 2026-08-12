@echo off
setlocal EnableDelayedExpansion
rem ==============================================================================
rem quotax — QuotaX 一键启动器（由 install.ps1 安装到 PATH）
rem
rem 用法：
rem   quotax          启动服务（已在运行则直接打开浏览器）
rem   quotax stop     停止后台服务
rem   quotax status   查看运行状态
rem   quotax log [N]  查看最近 N 行日志
rem ==============================================================================

set "PERM_DIR=%USERPROFILE%\QuotaX"
if defined QUOTAX_HOME set "PERM_DIR=%QUOTAX_HOME%"
set "PORT=8900"

rem QUOTAX_PORT 会进入 PowerShell 命令字符串，先用环境变量校验纯数字和范围，
rem 防止特殊字符改变 stop/status/start 的命令语义。
powershell -NoProfile -Command "$p=$env:QUOTAX_PORT; if ([string]::IsNullOrEmpty($p)) {$p='8900'}; if ($p -notmatch '^\d{1,5}$' -or [int]$p -lt 1 -or [int]$p -gt 65535) { exit 2 }" >nul 2>&1
if errorlevel 2 (
  echo Error: QUOTAX_PORT 必须是 1 到 65535 之间的整数。 1>&2
  exit /b 2
)
if defined QUOTAX_PORT for /f "delims=" %%P in ('powershell -NoProfile -Command "Write-Output $env:QUOTAX_PORT"') do set "PORT=%%P"
set "URL=http://127.0.0.1:%PORT%"

rem 判断是否存在命令行匹配 app.main:app 且端口一致的 QuotaX 进程。
call :is_running
set "RUNNING=%ERRORLEVEL%"

if "%~1"=="" goto :start
if /I "%~1"=="start" goto :start
if /I "%~1"=="stop" goto :stop
if /I "%~1"=="status" goto :status
if /I "%~1"=="log" goto :log
echo 用法: quotax [start^|stop^|status^|log] 1>&2
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
set "QUOTAX_RUN_DIR=%PERM_DIR%"
set "QUOTAX_LOG_FILE=%LOG_FILE%"
powershell -NoProfile -Command "$path=$env:QUOTAX_LOG_FILE; if ((Test-Path -LiteralPath $path) -and (Get-Item -LiteralPath $path).Length -gt 5MB) { Move-Item -LiteralPath $path -Destination ($path + '.1') -Force }" >nul 2>&1
start "" /B powershell -NoProfile -WindowStyle Hidden -Command ^
  "Set-Location -LiteralPath $env:QUOTAX_RUN_DIR; & uv run uvicorn app.main:app --host 127.0.0.1 --port %PORT% *>&1 | Tee-Object -FilePath $env:QUOTAX_LOG_FILE"
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
  powershell -NoProfile -Command "$portPattern='(?<!\S)--port(?:\s+|=)%PORT%(?=\s|$)'; $p=Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and (($_.CommandLine -like '*uvicorn*app.main:app*' -or $_.CommandLine -like '*app.main:app*uvicorn*') -and $_.CommandLine -match $portPattern) }; $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
  echo QuotaX 已停止
) else (
  echo QuotaX 未在运行
)
exit /b 0

:log
set "QUOTAX_LOG_FILE=%PERM_DIR%\quotax.log"
set "QUOTAX_LOG_TAIL=%~2"
powershell -NoProfile -Command "$path=$env:QUOTAX_LOG_FILE; $n=$env:QUOTAX_LOG_TAIL; if ([string]::IsNullOrEmpty($n)) {$n='50'}; if ($n -notmatch '^\d+$' -or [int]$n -lt 1 -or [int]$n -gt 10000) { Write-Error '日志行数必须是 1 到 10000 之间的整数'; exit 2 }; if (Test-Path -LiteralPath $path) { Get-Content -LiteralPath $path -Tail ([int]$n) } else { Write-Output ('无日志文件: ' + $path) }"
exit /b %ERRORLEVEL%

:status
if "%RUNNING%"=="0" (
  echo ✅ QuotaX 运行中 -^> %URL%
) else (
  echo ❌ QuotaX 未运行（用 quotax 启动）
)
exit /b 0

rem :is_running — 返回 ERRORLEVEL 0=运行中, 1=未运行
:is_running
powershell -NoProfile -Command "$portPattern='(?<!\S)--port(?:\s+|=)%PORT%(?=\s|$)'; $p=Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and (($_.CommandLine -like '*uvicorn*app.main:app*' -or $_.CommandLine -like '*app.main:app*uvicorn*') -and $_.CommandLine -match $portPattern) } | Select-Object -First 1; if ($p) { exit 0 } else { exit 1 }" >nul 2>&1
exit /b %ERRORLEVEL%
