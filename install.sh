#!/usr/bin/env bash

# ==============================================================================
# Script: install.sh (macOS/Linux remote installer)
# Usage:  curl -fsSL https://raw.githubusercontent.com/RunhuaHuang/quotax/main/install.sh | bash
#
# 行为：clone/下载 QuotaX 源码 → 安装到 ~/QuotaX → 装 uv 依赖 → 启动 Web 服务。
# 多源 fallback：GitHub 原生优先，失败依次走国内镜像代理，可用
#   QUOTAX_MIRROR=https://ghfast.top bash install.sh
# pin 指定镜像。升级时自动保留 config.json / usage.db / history/ 等用户数据。
# ==============================================================================

set -euo pipefail

REPO="RunhuaHuang/quotax"
BRANCH="main"
PERM_DIR="$HOME/QuotaX"
TMP_DIR=$(mktemp -d)
PORT="${QUOTAX_PORT:-8900}"
PID_FILE="$PERM_DIR/quotax.pid"

# 端口值会进入 shell 命令、curl URL 和 uvicorn 参数，必须先限制为纯数字的
# TCP 端口，避免环境变量中的特殊字符破坏安装流程或注入后续命令。
if ! [[ "$PORT" =~ ^[0-9]{1,5}$ ]] || ((10#$PORT < 1 || 10#$PORT > 65535)); then
  echo "Error: QUOTAX_PORT 必须是 1 到 65535 之间的整数。" >&2
  exit 2
fi

INSTALL_SWAPPED=0
OLD_DIR="$PERM_DIR"
BACKUP_DIR="$PERM_DIR.bak"
PREV_BACKUP="$PERM_DIR.bak.prev"
APP_PID=""

cleanup() { rm -rf "$TMP_DIR"; }

rollback_install() {
  local exit_code="$1"
  [ "$INSTALL_SWAPPED" -eq 1 ] || return 0

  # 失败时先停止本次新版本启动的进程，避免目录仍被占用或继续写入新数据。
  if [ -n "$APP_PID" ] && kill -0 "$APP_PID" 2>/dev/null; then
    kill "$APP_PID" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$APP_PID" 2>/dev/null || break
      sleep 0.2
    done
  fi

  if [ "$exit_code" -eq 0 ]; then
    # 只有完整安装、依赖同步并通过健康检查后才走这里。
    rm -rf "$PREV_BACKUP" "$BACKUP_DIR"
    INSTALL_SWAPPED=0
    return 0
  fi

  if [ -d "$BACKUP_DIR" ]; then
    local failed_dir="$PERM_DIR.failed"
    rm -rf "$failed_dir"
    # 第二次目录交换可能在 mv "$NEW_DIR" "$OLD_DIR" 处失败，此时
    # $PERM_DIR 根本不存在；不能让这一步的失败阻断旧版本恢复。
    if [ -e "$PERM_DIR" ] && ! mv "$PERM_DIR" "$failed_dir" 2>/dev/null; then
      echo "警告：无法移走失败的新目录 $PERM_DIR；旧版本仍保留在 $BACKUP_DIR。" >&2
      return 0
    fi
    if mv "$BACKUP_DIR" "$PERM_DIR" 2>/dev/null; then
      rm -rf "$failed_dir"
      rm -rf "$PERM_DIR.new"
      INSTALL_SWAPPED=0
      echo "已回滚到升级前版本；新版本临时目录已移除。" >&2
    else
      echo "警告：自动回滚未完成；旧版本备份仍位于 $BACKUP_DIR（请勿删除）。" >&2
    fi
  else
    # 首次安装没有旧版本可恢复，至少移除未完成的新目录，避免下次误用半成品。
    rm -rf "$PERM_DIR" "$PERM_DIR.new"
    INSTALL_SWAPPED=0
    echo "首次安装未完成，已移除不完整的安装目录。" >&2
  fi
}

on_exit() {
  local exit_code=$?
  trap - EXIT
  set +e
  rollback_install "$exit_code"
  cleanup
  exit "$exit_code"
}
trap on_exit EXIT

echo "============================================="
echo "QuotaX Remote Installer (macOS/Linux)"
echo "============================================="

# --- Download ---
# 多源 + 自动 fallback：GitHub 原生优先（海外用户最快）；失败/超时再依次尝试
# 国内友好的镜像代理，避免被墙用户装不上。镜像 URL 经常变动，所以逐个探测
# 而非信任单一源。可用 QUOTAX_MIRROR=<url-prefix> pin 指定镜像（覆盖整张列表）。
echo "Downloading QuotaX..."

ARCHIVE_PATH="/$REPO/archive/refs/heads/$BRANCH.tar.gz"
MIRROR_PREFIXES=(
  ""                                  # GitHub 原生（无前缀）
  "https://ghfast.top"                # ghfast 镜像代理
  "https://gh-proxy.com"              # gh-proxy 镜像代理
  "https://github.moeyy.xyz"          # moeyy 镜像代理
)
# 用户 pin 的镜像始终优先（替换整张列表）。
if [ -n "${QUOTAX_MIRROR:-}" ]; then
  MIRROR_PREFIXES=("$QUOTAX_MIRROR")
fi

SRC_DIR=""
# 首选：浅克隆（git 能用就优先走 git，体积更小、能拿到完整文件树）。
if command -v git &>/dev/null; then
  for _prefix in "${MIRROR_PREFIXES[@]}"; do
    # git clone 失败时可能留下部分目标目录；每个镜像尝试前清掉它，否则后续
    # fallback 会因“destination path already exists”而永远无法继续。
    rm -rf "$TMP_DIR/quotax"
    if [ -z "$_prefix" ]; then
      _clone_url="https://github.com/$REPO.git"
    else
      _clone_url="${_prefix}/https://github.com/$REPO.git"
    fi
    if git clone --depth 1 --branch "$BRANCH" "$_clone_url" "$TMP_DIR/quotax" 2>/dev/null; then
      SRC_DIR="$TMP_DIR/quotax"
      break
    fi
  done
fi
# 回退：curl 下载 tarball，逐个镜像尝试。
if [ -z "$SRC_DIR" ]; then
  for _prefix in "${MIRROR_PREFIXES[@]}"; do
    # 空前缀 = GitHub 原生；镜像代理把自己前缀拼到完整 github.com URL 前。
    if [ -z "$_prefix" ]; then
      _url="https://github.com${ARCHIVE_PATH}"
    else
      _url="${_prefix}/https://github.com${ARCHIVE_PATH}"
    fi
    if curl -fsSL --connect-timeout 15 "$_url" -o "$TMP_DIR/repo.tar.gz" 2>/dev/null; then
      if tar -xzf "$TMP_DIR/repo.tar.gz" -C "$TMP_DIR" 2>/dev/null; then
        SRC_DIR="$TMP_DIR/quotax-$BRANCH"
        break
      fi
    fi
  done
fi

# 所有源都失败：给出明确报错，而不是带着空的 SRC_DIR 继续往下走。
if [ -z "$SRC_DIR" ]; then
  echo "Error: 无法从 GitHub 或任何镜像下载 QuotaX。" >&2
  echo "       检查网络，或用指定镜像重试：" >&2
  echo "         QUOTAX_MIRROR=https://ghfast.top bash install.sh" >&2
  exit 1
fi

# --- 校验下载完整性 ---
# 下载失败/不完整绝不能破坏已有安装。校验关键文件存在。
if [ ! -f "$SRC_DIR/app/main.py" ] || [ ! -f "$SRC_DIR/pyproject.toml" ]; then
  echo "Error: 下载的源码不完整（网络/GitHub 故障？）。" >&2
  echo "       $PERM_DIR 的已有安装未受影响。" >&2
  exit 1
fi

# 升级前先停止确认属于 QuotaX 的旧进程，避免复制 SQLite/WAL 时仍有写入，也避免
# Windows/Unix 上旧进程继续把配置写到刚交换的新目录。绝不因“端口相同”杀无关服务。
stop_existing_quotax() {
  local candidates=() pid command
  if [ -f "$PID_FILE" ]; then
    pid=$(tr -cd '0-9' < "$PID_FILE")
    [ -n "$pid" ] && candidates+=("$pid")
  fi
  if command -v lsof &>/dev/null; then
    while IFS= read -r pid; do
      [ -n "$pid" ] && candidates+=("$pid")
    done < <(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
  fi
  for pid in "${candidates[@]}"; do
    kill -0 "$pid" 2>/dev/null || continue
    command=$(ps -p "$pid" -o command= 2>/dev/null || true)
    if is_quotax_command "$command"; then
        echo "正在停止旧版 QuotaX（PID $pid）..."
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 10); do
          kill -0 "$pid" 2>/dev/null || break
          sleep 0.2
        done
    fi
  done
  rm -f "$PID_FILE"
}

is_quotax_command() {
  local command="$1"
  case "$command" in
    *uvicorn*app.main:app*|*app.main:app*uvicorn*) ;;
    *) return 1 ;;
  esac
  local port_pattern="(^|[[:space:]])--port([[:space:]]+|=)${PORT}($|[[:space:]])"
  [[ "$command" =~ $port_pattern ]]
}
stop_existing_quotax

# --- 备份用户数据（升级前保护 config.json / usage.db / history/）---
# 这些是 .gitignore 排除的个人数据，绝不能被覆盖丢失。
PRESERVE_DIR="$TMP_DIR/preserve"
mkdir -p "$PRESERVE_DIR"
PRESERVE_FILES=(config.json monitor-state.json usage.db usage.db-shm usage.db-wal)
PRESERVE_DIRS=(history credentials)
if [ -d "$PERM_DIR" ]; then
  for f in "${PRESERVE_FILES[@]}"; do
    [ -f "$PERM_DIR/$f" ] && cp "$PERM_DIR/$f" "$PRESERVE_DIR/$f"
  done
  for d in "${PRESERVE_DIRS[@]}"; do
    [ -d "$PERM_DIR/$d" ] && cp -R "$PERM_DIR/$d" "$PRESERVE_DIR/$d"
  done
  echo "已备份现有用户数据（config.json / monitor-state.json / usage.db / history/）。"
fi

# --- 原子安装：先拷到临时目录，校验后再交换，避免拷贝中途失败导致安装损坏 ---
NEW_DIR="$PERM_DIR.new"
rm -rf "$NEW_DIR"
mkdir -p "$NEW_DIR"
# 用 rsync 拷贝（如有），否则回退 cp -R；排除 .git 等版本控制目录。
if command -v rsync &>/dev/null; then
  rsync -a --exclude='.git' "$SRC_DIR/" "$NEW_DIR/"
else
  cp -R "$SRC_DIR/." "$NEW_DIR/"
  rm -rf "$NEW_DIR/.git"
fi
# 校验拷贝结果可用再交换。
if [ ! -f "$NEW_DIR/app/main.py" ]; then
  echo "Error: 拷贝源码失败（磁盘满？权限？）。" >&2
  rm -rf "$NEW_DIR"
  echo "       $PERM_DIR 的已有安装未受影响。" >&2
  exit 1
fi

# 交换：当前 → .bak，新 → 当前。失败有回滚，避免出现「没有可用安装」的窗口。
rm -rf "$PREV_BACKUP"
if [ -d "$OLD_DIR" ]; then
  [ -d "$BACKUP_DIR" ] && mv "$BACKUP_DIR" "$PREV_BACKUP"
  mv "$OLD_DIR" "$BACKUP_DIR"
fi
# 从这一步开始即进入可回滚状态：如果第二次 mv 失败，旧版本已经位于
# .bak，不能等到两次 mv 都成功后才设置标记，否则 set -e 退出时会留下
# “当前目录不存在、旧版本只在 .bak”的半安装状态。
INSTALL_SWAPPED=1
mv "$NEW_DIR" "$OLD_DIR"
echo "已安装到 $PERM_DIR"

# --- 恢复用户数据 ---
RESTORE_FAILED=0
for f in "${PRESERVE_FILES[@]}"; do
  if [ -f "$PRESERVE_DIR/$f" ]; then
    if ! cp "$PRESERVE_DIR/$f" "$PERM_DIR/$f"; then
      RESTORE_FAILED=1
      break
    fi
    [ "$f" = "config.json" ] && chmod 600 "$PERM_DIR/$f" 2>/dev/null || true
  fi
done
if [ "$RESTORE_FAILED" -eq 0 ]; then
  for d in "${PRESERVE_DIRS[@]}"; do
    if [ -d "$PRESERVE_DIR/$d" ] && ! cp -R "$PRESERVE_DIR/$d" "$PERM_DIR/$d"; then
      RESTORE_FAILED=1
      break
    fi
  done
fi
if [ "$RESTORE_FAILED" -eq 0 ] && [ -d "$PERM_DIR/credentials" ]; then
  # 凭据目录和 auth.json 都是敏感数据；升级恢复后再次显式收紧权限，避免
  # cp/umask/跨文件系统复制让旧的宽松权限被带入新安装。
  chmod 700 "$PERM_DIR/credentials" 2>/dev/null || true
  find "$PERM_DIR/credentials" -type d -exec chmod 700 {} + 2>/dev/null || true
  find "$PERM_DIR/credentials" -type f -name 'codex_*.json' -exec chmod 600 {} + 2>/dev/null || true
fi
if [ "$RESTORE_FAILED" -ne 0 ]; then
  echo "Error: 用户数据恢复失败，正在回滚到升级前版本。" >&2
  exit 1
fi

# --- 安装 uv（如未装）---
# QuotaX 依赖 uv 管理 Python 环境与依赖。uv 官方一键脚本会装到 ~/.local/bin。
install_uv() {
  echo "未检测到 uv，正在安装..."
  # 先试官方脚本（需 curl）；失败则按平台给 Homebrew/手册提示。
  if command -v curl &>/dev/null; then
    if curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null; then
      # uv 装到 ~/.local/bin，可能不在当前 PATH，手动 sourcing。
      export PATH="$HOME/.local/bin:$PATH"
      return 0
    fi
  fi
  echo "Error: uv 自动安装失败。请手动安装 uv 后重试：" >&2
  echo "  macOS:  brew install uv" >&2
  echo "  Linux:  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  echo "  或访问  https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
}
if ! command -v uv &>/dev/null; then
  # 装完后可能 PATH 没更新，补查 ~/.local/bin。
  if [ -x "$HOME/.local/bin/uv" ]; then
    export PATH="$HOME/.local/bin:$PATH"
  else
    install_uv
  fi
fi
echo "uv: $(uv --version)"

# --- 安装 quotax 命令到 ~/.local/bin（与 uv 同目录，通常已在 PATH）---
# 之后用户只需输入 quotax 即可启动 / 打开 WebUI。
LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"
cp "$PERM_DIR/quotax" "$LOCAL_BIN/quotax"
chmod +x "$LOCAL_BIN/quotax"
export PATH="$LOCAL_BIN:$PATH"

# 检查 ~/.local/bin 是否在 shell 的 PATH 里；不在则提示用户加一行。
if ! echo ":$PATH:" | grep -q ":$LOCAL_BIN:"; then
  echo ""
  echo "⚠️  提示：$LOCAL_BIN 不在你的 PATH 中。quotax 命令需要它在 PATH。"
  echo "   请在 shell 配置文件里加一行（然后重开终端）："
  echo "     export PATH=\"$LOCAL_BIN:\$PATH\""
  echo ""
fi

# --- 确保 Python 运行时：优先复用本机已有的 3.11+，没有才装 3.13 ---
# 这样已有 Python（3.11/3.12/3.13/3.14）的用户不会被强制再下一个；只有本机
# 完全没有满足条件的 Python 时，才显式装 3.13（与你系统一致，uv 官方维护）。
cd "$PERM_DIR"
if uv python find ">=3.11" >/dev/null 2>&1; then
  PY_VER=$(uv python find ">=3.11" --show-version 2>/dev/null | tail -1)
  echo "检测到本机已有 Python ${PY_VER:-（3.11+）}，直接复用。"
else
  echo "本机没有满足条件的 Python（需要 3.11+），正在安装 Python 3.13..."
  uv python install 3.13
fi

# --- 同步依赖（uv sync 按 uv.lock 精确安装，复用上一步确定的 Python）---
echo "安装依赖（首次需下载 FastAPI / httpx 等，请稍候）..."
uv sync --quiet

# --- 启动服务 ---
# 后台启动 uvicorn，输出重定向到日志文件；端口被占用时给出提示。
LOG_FILE="$PERM_DIR/quotax.log"
echo "启动 QuotaX（端口 $PORT）..."

# 简单轮转：每次启动前若日志超过 5 MiB，保留一份 .1，避免长期后台运行无限增长。
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 5242880 ]; then
  mv -f "$LOG_FILE" "$LOG_FILE.1"
fi

# 旧 QuotaX 已在升级前停止；若端口仍占用，则它属于别的程序，绝不能强杀。
if command -v lsof &>/dev/null; then
  OCCUPIED_PID=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
  if [ -n "$OCCUPIED_PID" ]; then
    echo "Error: 端口 $PORT 正被其它程序占用（PID $OCCUPIED_PID），未启动 QuotaX。" >&2
    exit 1
  fi
fi

# nohup 后台启动，退出本脚本后服务继续运行。
nohup uv run uvicorn app.main:app --host 127.0.0.1 --port "$PORT" > "$LOG_FILE" 2>&1 &
APP_PID=$!
echo "$APP_PID" > "$PID_FILE"

# 等待服务就绪（最多 30 秒轮询 /health 或 TCP 端口）。
echo -n "等待服务就绪"
for _ in $(seq 1 30); do
  # 先确认本次启动的进程仍在，再访问带业务语义的健康端点；不能只看首页
  # HTTP 200，否则端口上已有其它 Web 服务时会被误判为 QuotaX 已启动。
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo ""
    echo "Error: 服务启动失败，日志见 $LOG_FILE：" >&2
    tail -20 "$LOG_FILE" >&2 2>/dev/null || true
    exit 1
  fi
  if curl -fsS "http://127.0.0.1:$PORT/api/health" -o "$TMP_DIR/health.json" 2>/dev/null \
    && grep -Eq '"ok"[[:space:]]*:[[:space:]]*true' "$TMP_DIR/health.json"; then
    echo " ✅"
    READY=1
    break
  fi
  echo -n "."
  sleep 1
done
echo ""

# --- 尝试打开浏览器 ---
if [ -z "${READY:-}" ]; then
  echo "Error: 服务在 30 秒内未通过健康检查，日志见 $LOG_FILE。" >&2
  tail -20 "$LOG_FILE" >&2 2>/dev/null || true
  exit 1
fi

# 新版本、用户数据、依赖同步和服务健康检查都成功后，才清理旧版本备份。
rm -rf "$PREV_BACKUP" "$BACKUP_DIR"
INSTALL_SWAPPED=0

# macOS 用 open，Linux 试 xdg-open。
(command -v open &>/dev/null && open "http://127.0.0.1:$PORT") \
  || (command -v xdg-open &>/dev/null && xdg-open "http://127.0.0.1:$PORT") \
  || true

echo ""
echo "============================================="
echo "✅ QuotaX 已启动"
echo "   地址:  http://127.0.0.1:$PORT"
echo "   日志:  $LOG_FILE"
echo "   目录:  $PERM_DIR"
echo ""
echo "   下次打开只需在终端输入:  quotax"
echo "   停止服务:               quotax stop"
echo "   查看状态:               quotax status"
echo "============================================="
