# ==============================================================================
# Script: install.ps1 (Windows remote installer)
# Usage:  irm https://raw.githubusercontent.com/RunhuaHuang/quotax/main/install.ps1 | iex
#
# 行为：下载 QuotaX 源码 → 安装到 %USERPROFILE%\QuotaX → 装 uv 依赖 → 启动 Web 服务。
# 多源 fallback：GitHub 原生优先，失败依次走国内镜像代理，可用
#   $env:QUOTAX_MIRROR='https://ghfast.top'; irm ... | iex
# pin 指定镜像。升级时自动保留 config.json / usage.db / history/ 等用户数据。
# ==============================================================================

$ErrorActionPreference = "Stop"

$Repo = "RunhuaHuang/quotax"
$Branch = "main"
$PermDir = "$env:USERPROFILE\QuotaX"
$TmpDir = Join-Path ([System.IO.Path]::GetTempPath()) "quotax-install-$(Get-Random)"
$Port = if ($env:QUOTAX_PORT) { $env:QUOTAX_PORT } else { "8900" }
$PreserveDir = $null
$BackupDir = "$PermDir.bak"
$PrevBackup = "$PermDir.bak.prev"
$NewDir = "$PermDir.new"
$InstallSwapped = $false
$Proc = $null

# 端口值会被拼入 PowerShell/uvicorn/curl 命令，必须先限制为纯数字，避免
# QUOTAX_PORT 中的特殊字符改变后续命令语义。
if ($Port -notmatch '^\d{1,5}$') {
  throw "QUOTAX_PORT 必须是 1 到 65535 之间的整数。"
}
$PortNumber = [int]$Port
if ($PortNumber -lt 1 -or $PortNumber -gt 65535) {
  throw "QUOTAX_PORT 必须是 1 到 65535 之间的整数。"
}
$Port = $PortNumber

function Cleanup { if (Test-Path $TmpDir) { Remove-Item $TmpDir -Recurse -Force -ErrorAction SilentlyContinue } }

function Rollback-Install {
  if (-not $InstallSwapped) { return }

  if ($Proc -and -not $Proc.HasExited) {
    try { Stop-Process -Id $Proc.Id -Force -ErrorAction SilentlyContinue } catch {}
  }

  if (Test-Path $BackupDir) {
    $FailedDir = "$PermDir.failed"
    if (Test-Path $FailedDir) { Remove-Item $FailedDir -Recurse -Force -ErrorAction SilentlyContinue }
    try {
      Move-Item -Path $PermDir -Destination $FailedDir -Force -ErrorAction Stop
      Move-Item -Path $BackupDir -Destination $PermDir -Force -ErrorAction Stop
      Remove-Item $FailedDir -Recurse -Force -ErrorAction SilentlyContinue
      Write-Host "已回滚到升级前版本；新版本临时目录已移除。" -ForegroundColor Yellow
      $script:InstallSwapped = $false
    } catch {
      Write-Warning "自动回滚未完成；旧版本备份仍位于 $BackupDir（请勿删除）。$($_.Exception.Message)"
    }
  } else {
    if (Test-Path $PermDir) { Remove-Item $PermDir -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path $NewDir) { Remove-Item $NewDir -Recurse -Force -ErrorAction SilentlyContinue }
    Write-Host "首次安装未完成，已移除不完整的安装目录。" -ForegroundColor Yellow
    $script:InstallSwapped = $false
  }
}

try {
  Write-Host "=============================================" -ForegroundColor Cyan
  Write-Host "QuotaX Remote Installer (Windows)" -ForegroundColor Cyan
  Write-Host "=============================================" -ForegroundColor Cyan

  # --- Download ---
  # 多源 + 自动 fallback：GitHub 原生优先；失败/超时再依次尝试国内友好的镜像
  # 代理，避免被墙用户装不上。镜像 URL 经常变动，所以逐个探测而非信任单一
  # 源。可用 $env:QUOTAX_MIRROR pin 指定镜像（覆盖整张列表）。
  Write-Host "Downloading QuotaX..."
  New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null
  $ZipPath = Join-Path $TmpDir "repo.zip"
  $ArchivePath = "/$Repo/archive/refs/heads/$Branch.zip"
  # GitHub 原生优先，然后国内镜像代理。
  $MirrorPrefixes = @("", "https://ghfast.top", "https://gh-proxy.com", "https://github.moeyy.xyz")
  # 用户 pin 的镜像始终优先（替换整张列表）。
  if ($env:QUOTAX_MIRROR) { $MirrorPrefixes = @($env:QUOTAX_MIRROR) }

  $Downloaded = $false
  foreach ($Prefix in $MirrorPrefixes) {
    # 空前缀 = GitHub 原生；镜像代理把自己前缀拼到完整 github.com URL 前。
    if ($Prefix) {
      $ZipUrl = "${Prefix}/https://github.com${ArchivePath}"
    } else {
      $ZipUrl = "https://github.com${ArchivePath}"
    }
    try {
      # 下载/解压失败可能留下半截 zip 或目标目录；每次尝试前清理临时产物，
      # 避免后续镜像误读上一轮的残留文件。
      if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force -ErrorAction SilentlyContinue }
      $ExtractedDir = Join-Path $TmpDir "quotax-$Branch"
      if (Test-Path $ExtractedDir) { Remove-Item $ExtractedDir -Recurse -Force -ErrorAction SilentlyContinue }
      Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipPath -UseBasicParsing -TimeoutSec 30 -ErrorAction Stop
      Expand-Archive -Path $ZipPath -DestinationPath $TmpDir -Force -ErrorAction Stop
      $Downloaded = $true
      break
    } catch {
      # 此源失败，静默尝试下一个镜像。
    }
  }
  if (-not $Downloaded) {
    throw "无法从 GitHub 或任何镜像下载 QuotaX。检查网络，或 pin 指定镜像: `$env:QUOTAX_MIRROR='https://ghfast.top'"
  }
  $SrcDir = Join-Path $TmpDir "quotax-$Branch"

  # --- 校验下载完整性 ---
  # 下载失败/不完整绝不能破坏已有安装。校验关键文件存在。
  $SrcMain = Join-Path $SrcDir "app\main.py"
  $SrcPyproject = Join-Path $SrcDir "pyproject.toml"
  if (-not (Test-Path $SrcMain) -or -not (Test-Path $SrcPyproject)) {
    throw "下载的源码不完整（网络/GitHub 故障？）。已有安装未受影响。"
  }

  # 升级前停止确认属于 QuotaX 的旧进程，避免 SQLite/WAL 复制竞态和目录交换失败。
  $PortRegex = "(?<!\S)--port(?:\s+|=)$Port(?=\s|$)"
  $OldQuotaX = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
      $_.ProcessId -ne $PID -and $_.CommandLine -and
      (($_.CommandLine -like "*uvicorn*app.main:app*" -or $_.CommandLine -like "*app.main:app*uvicorn*") -and
       $_.CommandLine -match $PortRegex)
    }
  foreach ($P in $OldQuotaX) {
    Write-Host "正在停止旧版 QuotaX（PID $($P.ProcessId)）..."
    try { $P | Invoke-CimMethod -MethodName Terminate -ErrorAction Stop | Out-Null } catch {}
  }
  if ($OldQuotaX) { Start-Sleep -Seconds 1 }

  # --- 备份用户数据（升级前保护 config.json / usage.db / history/）---
  # 这些是 .gitignore 排除的个人数据，绝不能被覆盖丢失。
  $PreserveDir = Join-Path $TmpDir "preserve"
  New-Item -ItemType Directory -Path $PreserveDir -Force | Out-Null
  $PreserveFiles = @("config.json", "monitor-state.json", "usage.db", "usage.db-shm", "usage.db-wal")
  $PreserveDirs = @("history", "credentials")
  if (Test-Path $PermDir) {
    foreach ($f in $PreserveFiles) {
      $src = Join-Path $PermDir $f
      if (Test-Path $src) { Copy-Item $src (Join-Path $PreserveDir $f) -Force }
    }
    foreach ($d in $PreserveDirs) {
      $src = Join-Path $PermDir $d
      if (Test-Path $src) { Copy-Item $src (Join-Path $PreserveDir $d) -Recurse -Force }
    }
    Write-Host "已备份现有用户数据（config.json / monitor-state.json / usage.db / history/）。"
  }

  # --- 原子安装：先拷到临时目录，校验后再交换，避免拷贝中途失败导致安装损坏 ---
  if (Test-Path $NewDir) { Remove-Item $NewDir -Recurse -Force }
  New-Item -ItemType Directory -Path $NewDir -Force | Out-Null
  # 拷贝源码到临时目录，排除 .git。
  Get-ChildItem -Path $SrcDir -Force | Where-Object { $_.Name -ne ".git" } | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $NewDir $_.Name) -Recurse -Force
  }
  # 校验拷贝结果可用再交换。
  $NewMain = Join-Path $NewDir "app\main.py"
  if (-not (Test-Path $NewMain)) {
    if (Test-Path $NewDir) { Remove-Item $NewDir -Recurse -Force }
    throw "拷贝源码失败（磁盘满？权限？）。已有安装未受影响。"
  }

  # 交换：当前 → .bak，新 → 当前。用 Move-Item（而非 Rename-Item），失败有回滚。
  if (Test-Path $PrevBackup) { Remove-Item $PrevBackup -Recurse -Force }
  try {
    if (Test-Path $PermDir) {
      if (Test-Path $BackupDir) { Move-Item -Path $BackupDir -Destination $PrevBackup -Force }
      Move-Item -Path $PermDir -Destination $BackupDir -Force
    }
    Move-Item -Path $NewDir -Destination $PermDir -Force
    $InstallSwapped = $true
  } catch {
    if (Test-Path $NewDir) { Remove-Item $NewDir -Recurse -Force -ErrorAction SilentlyContinue }
    if ((-not (Test-Path $PermDir)) -and (Test-Path $BackupDir)) {
      Move-Item -Path $BackupDir -Destination $PermDir -Force -ErrorAction SilentlyContinue
    }
    throw "安装交换失败，已尽量回滚。$($_.Exception.Message)"
  }
  Write-Host "已安装到 $PermDir"

  # --- 恢复用户数据 ---
  # 任一复制失败都交给外层 catch 的统一回滚处理，避免这里先回滚、外层又把已恢复的
  # 旧版本目录误当作“新目录”再次删除。
  foreach ($f in $PreserveFiles) {
    $src = Join-Path $PreserveDir $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $PermDir $f) -Force -ErrorAction Stop }
  }
  foreach ($d in $PreserveDirs) {
    $src = Join-Path $PreserveDir $d
    if (Test-Path $src) { Copy-Item $src (Join-Path $PermDir $d) -Recurse -Force -ErrorAction Stop }
  }

  # --- 安装 uv（如未装）---
  # QuotaX 依赖 uv 管理 Python 环境与依赖。优先用 winget，回退官方 PowerShell 安装器。
  $UvCmd = Get-Command uv -ErrorAction SilentlyContinue
  if (-not $UvCmd) {
    # 补查 uv 常见安装路径（官方脚本装到 ~/.local/bin 或 cargo bin）。
    $UvCandidates = @(
      "$env:USERPROFILE\.local\bin\uv.exe",
      "$env:USERPROFILE\.cargo\bin\uv.exe"
    )
    foreach ($c in $UvCandidates) {
      if (Test-Path $c) { $env:PATH = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"; $UvCmd = Get-Command uv -ErrorAction SilentlyContinue; break }
    }
  }
  if (-not $UvCmd) {
    Write-Host "未检测到 uv，正在安装..."
    $UvInstalled = $false
    # 方式一：winget（Windows 10/11 自带，最省事）。
    $Winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($Winget) {
      try {
        & winget install --id=astral-sh.uv -e --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
        # winget 装的 uv 需要刷新 PATH（当前会话手动补）。
        $env:PATH = "$env:USERPROFILE\.local\bin;$env:ProgramFiles\uv;$env:PATH"
        $UvCmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($UvCmd) { $UvInstalled = $true }
      } catch {}
    }
    # 方式二：官方 PowerShell 安装器。
    if (-not $UvInstalled) {
      try {
        $InstallScript = Join-Path $TmpDir "uv-install.ps1"
        Invoke-WebRequest -Uri "https://astral.sh/uv/install.ps1" -OutFile $InstallScript -UseBasicParsing -TimeoutSec 30
        & powershell -NoProfile -ExecutionPolicy Bypass -File $InstallScript 2>&1 | Out-Null
        $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
        $UvCmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($UvCmd) { $UvInstalled = $true }
      } catch {}
    }
    if (-not $UvInstalled) {
      throw "uv 自动安装失败。请手动安装 uv 后重试: winget install astral-sh.uv，或访问 https://docs.astral.sh/uv/getting-started/installation/"
    }
  }
  Write-Host "uv: $(& uv --version)"

  # --- 安装 quotax 命令到 PATH（%USERPROFILE%\.local\bin，通常 uv 已加入 PATH）---
  # 之后用户只需在任意终端输入 quotax 即可启动 / 打开 WebUI。
  $LocalBin = "$env:USERPROFILE\.local\bin"
  if (-not (Test-Path $LocalBin)) { New-Item -ItemType Directory -Path $LocalBin -Force | Out-Null }
  Copy-Item (Join-Path $PermDir "quotax.cmd") (Join-Path $LocalBin "quotax.cmd") -Force

  # 检查 ~/.local/bin 是否在用户 PATH；不在则尝试加入用户级 PATH。
  $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if ($UserPath -notlike "*$LocalBin*") {
    [Environment]::SetEnvironmentVariable("Path", "$LocalBin;$UserPath", "User")
    Write-Host "已将 $LocalBin 加入用户 PATH（重开终端后生效）"
  }

  # --- 确保 Python 运行时：优先复用本机已有的 3.11+，没有才装 3.13 ---
  # 这样已有 Python（3.11/3.12/3.13/3.14）的用户不会被强制再下一个；只有本机
  # 完全没有满足条件的 Python 时，才显式装 3.13（与 macOS 系统一致，uv 官方维护）。
  Push-Location $PermDir
  try {
    $FoundPy = & uv python find ">=3.11" 2>$null
    if ($FoundPy) {
      $PyVer = & uv python find ">=3.11" --show-version 2>$null | Select-Object -Last 1
      Write-Host "检测到本机已有 Python $($PyVer.Trim())（$FoundPy），直接复用。"
    } else {
      Write-Host "本机没有满足条件的 Python（需要 3.11+），正在安装 Python 3.13..."
      & uv python install 3.13
      if ($LASTEXITCODE -ne 0) { throw "uv python install 失败（退出码 $LASTEXITCODE）" }
    }
    # --- 同步依赖（uv sync 按 uv.lock 精确安装，复用上一步确定的 Python）---
    Write-Host "安装依赖（首次需下载 FastAPI / httpx 等，请稍候）..."
    & uv sync --quiet
    if ($LASTEXITCODE -ne 0) { throw "uv sync 失败（退出码 $LASTEXITCODE）" }
  } finally {
    Pop-Location
  }

  # --- 启动服务 ---
  $LogFile = Join-Path $PermDir "quotax.log"
  Write-Host "启动 QuotaX（端口 $Port）..."

  # 每次启动前最多保留一份 5 MiB 以上的旧日志，避免长期运行无限增长。
  if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 5MB) {
    Move-Item $LogFile "$LogFile.1" -Force
  }

  # 旧 QuotaX 已在升级前停止；此时端口若仍占用，属于其它程序，不能强杀。
  $PortOwner = Get-NetTCPConnection -LocalPort ([int]$Port) -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if ($PortOwner) { throw "端口 $Port 正被其它程序占用（PID $($PortOwner.OwningProcess)），未启动 QuotaX。" }

  # 后台启动 uvicorn（无窗口），输出重定向到日志。
  # Do not interpolate user-controlled filesystem paths into a quoted
  # PowerShell command: an apostrophe in %USERPROFILE% (or in a custom path)
  # would terminate the string. Child PowerShell inherits these environment
  # variables, while Start-Process supplies the working directory safely.
  $env:QUOTAX_RUN_DIR = $PermDir
  $env:QUOTAX_LOG_FILE = $LogFile
  $ArgumentList = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command",
    "& uv run uvicorn app.main:app --host 127.0.0.1 --port $Port *>&1 | Tee-Object -FilePath `$env:QUOTAX_LOG_FILE"
  )
  $Proc = Start-Process -FilePath "powershell.exe" -ArgumentList $ArgumentList `
    -WorkingDirectory $PermDir -WindowStyle Hidden -PassThru

  # 等待服务就绪（最多 30 秒轮询 HTTP）。
  Write-Host "等待服务就绪" -NoNewline
  $Ready = $false
  for ($i = 1; $i -le 30; $i++) {
    try {
      if ($Proc.HasExited) {
        throw "服务进程已退出"
      }
      $Health = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
      $HealthJson = $Health.Content | ConvertFrom-Json -ErrorAction Stop
      if ($HealthJson.ok -ne $true) {
        throw "健康检查返回 ok=false"
      }
      Write-Host " ✅" -ForegroundColor Green
      $Ready = $true
      break
    } catch {
      # 进程已退出则提前报错。
      if ($Proc.HasExited) {
        Write-Host ""
        Write-Host "Error: 服务启动失败，日志见 $LogFile" -ForegroundColor Red
        if (Test-Path $LogFile) { Get-Content $LogFile -Tail 20 | Write-Host }
        throw "服务进程已退出"
      }
      Write-Host "." -NoNewline
      Start-Sleep -Seconds 1
    }
  }
  Write-Host ""

  if (-not $Ready) {
    if (Test-Path $LogFile) { Get-Content $LogFile -Tail 20 | Write-Host }
    throw "服务在 30 秒内未通过健康检查，日志见 $LogFile"
  }

  # 新版本、用户数据、依赖同步和服务健康检查都成功后，才清理旧版本备份。
  if (Test-Path $PrevBackup) { Remove-Item $PrevBackup -Recurse -Force }
  if (Test-Path $BackupDir) { Remove-Item $BackupDir -Recurse -Force }
  $InstallSwapped = $false

  # --- 尝试打开浏览器 ---
  Start-Process "http://127.0.0.1:$Port"

  Write-Host ""
  Write-Host "=============================================" -ForegroundColor Green
  Write-Host "✅ QuotaX 已启动"
  Write-Host "   地址: http://127.0.0.1:$Port"
  Write-Host "   日志: $LogFile"
  Write-Host "   目录: $PermDir"
  Write-Host ""
  Write-Host "   下次打开只需在终端输入:  quotax"
  Write-Host "   停止服务:               quotax stop"
  Write-Host "   查看状态:               quotax status"
  Write-Host "=============================================" -ForegroundColor Green
} catch {
  Rollback-Install
  throw
} finally {
  Cleanup
}
