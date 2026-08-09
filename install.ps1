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

function Cleanup { if (Test-Path $TmpDir) { Remove-Item $TmpDir -Recurse -Force -ErrorAction SilentlyContinue } }

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

  # --- 备份用户数据（升级前保护 config.json / usage.db / history/）---
  # 这些是 .gitignore 排除的个人数据，绝不能被覆盖丢失。
  $PreserveDir = Join-Path ([System.IO.Path]::GetTempPath()) ("quotax-preserve-" + [Guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Path $PreserveDir -Force | Out-Null
  $PreserveFiles = @("config.json", "usage.db", "usage.db-shm", "usage.db-wal")
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
    Write-Host "已备份现有用户数据（config.json / usage.db / history/）。"
  }

  # --- 原子安装：先拷到临时目录，校验后再交换，避免拷贝中途失败导致安装损坏 ---
  $NewDir = "$PermDir.new"
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
  $BackupDir = "$PermDir.bak"
  $PrevBackup = "$PermDir.bak.prev"
  if (Test-Path $PrevBackup) { Remove-Item $PrevBackup -Recurse -Force }
  try {
    if (Test-Path $PermDir) {
      if (Test-Path $BackupDir) { Move-Item -Path $BackupDir -Destination $PrevBackup -Force }
      Move-Item -Path $PermDir -Destination $BackupDir -Force
    }
    Move-Item -Path $NewDir -Destination $PermDir -Force
    if (Test-Path $PrevBackup) { Remove-Item $PrevBackup -Recurse -Force }
  } catch {
    if (Test-Path $NewDir) { Remove-Item $NewDir -Recurse -Force -ErrorAction SilentlyContinue }
    if ((-not (Test-Path $PermDir)) -and (Test-Path $BackupDir)) {
      Move-Item -Path $BackupDir -Destination $PermDir -Force -ErrorAction SilentlyContinue
    }
    throw "安装交换失败，已尽量回滚。$($_.Exception.Message)"
  }
  Write-Host "已安装到 $PermDir"

  # --- 恢复用户数据 ---
  foreach ($f in $PreserveFiles) {
    $src = Join-Path $PreserveDir $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $PermDir $f) -Force }
  }
  foreach ($d in $PreserveDirs) {
    $src = Join-Path $PreserveDir $d
    if (Test-Path $src) { Copy-Item $src (Join-Path $PermDir $d) -Recurse -Force }
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

  # --- 同步依赖（uv sync 按 uv.lock 精确安装，含 Python 3.13）---
  Write-Host "安装依赖（首次可能需要下载 Python 3.13，请稍候）..."
  Push-Location $PermDir
  try { & uv sync --quiet 2>&1 | Out-Null } catch {}
  Pop-Location

  # --- 启动服务 ---
  $LogFile = Join-Path $PermDir "quotax.log"
  Write-Host "启动 QuotaX（端口 $Port）..."

  # 若端口被旧进程占用，先结束（匹配命令行含 quotax 的 python/uvicorn 进程）。
  try {
    $Stale = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -and $_.CommandLine -like "*app.main:app*" -and $_.CommandLine -like "*$Port*" }
    foreach ($P in $Stale) { try { $P | Invoke-CimMethod -MethodName Terminate -ErrorAction SilentlyContinue | Out-Null } catch {} }
    if ($Stale) { Start-Sleep -Seconds 1 }
  } catch {}

  # 后台启动 uvicorn（无窗口），输出重定向到日志。
  $ArgumentList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command",
    "Set-Location '$PermDir'; & uv run uvicorn app.main:app --host 127.0.0.1 --port $Port *>&1 | Tee-Object -FilePath '$LogFile'")
  $Proc = Start-Process -FilePath "powershell.exe" -ArgumentList $ArgumentList -WindowStyle Hidden -PassThru

  # 等待服务就绪（最多 30 秒轮询 HTTP）。
  Write-Host "等待服务就绪" -NoNewline
  $Ready = $false
  for ($i = 1; $i -le 30; $i++) {
    try {
      Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop | Out-Null
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

  # --- 尝试打开浏览器 ---
  if (-not $Ready) {
    Write-Host "⚠️  服务仍在启动中（超过 30 秒），稍后访问 http://127.0.0.1:$Port"
    Write-Host "   日志: $LogFile"
  } else {
    Start-Process "http://127.0.0.1:$Port"
  }

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
} finally {
  Cleanup
}
