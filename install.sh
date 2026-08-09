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

set -e

REPO="RunhuaHuang/quotax"
BRANCH="main"
PERM_DIR="$HOME/QuotaX"
TMP_DIR=$(mktemp -d)
PORT="${QUOTAX_PORT:-8900}"

cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

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

# --- 备份用户数据（升级前保护 config.json / usage.db / history/）---
# 这些是 .gitignore 排除的个人数据，绝不能被覆盖丢失。
PRESERVE_DIR="$TMP_DIR/preserve"
mkdir -p "$PRESERVE_DIR"
PRESERVE_FILES=(config.json usage.db usage.db-shm usage.db-wal)
PRESERVE_DIRS=(history credentials)
if [ -d "$PERM_DIR" ]; then
  for f in "${PRESERVE_FILES[@]}"; do
    [ -f "$PERM_DIR/$f" ] && cp "$PERM_DIR/$f" "$PRESERVE_DIR/$f"
  done
  for d in "${PRESERVE_DIRS[@]}"; do
    [ -d "$PERM_DIR/$d" ] && cp -R "$PERM_DIR/$d" "$PRESERVE_DIR/$d"
  done
  echo "已备份现有用户数据（config.json / usage.db / history/）。"
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
OLD_DIR="$PERM_DIR"
BACKUP_DIR="$PERM_DIR.bak"
PREV_BACKUP="$PERM_DIR.bak.prev"
rm -rf "$PREV_BACKUP"
if [ -d "$OLD_DIR" ]; then
  [ -d "$BACKUP_DIR" ] && mv "$BACKUP_DIR" "$PREV_BACKUP"
  mv "$OLD_DIR" "$BACKUP_DIR"
fi
mv "$NEW_DIR" "$OLD_DIR"
rm -rf "$PREV_BACKUP"
echo "已安装到 $PERM_DIR"

# --- 恢复用户数据 ---
for f in "${PRESERVE_FILES[@]}"; do
  if [ -f "$PRESERVE_DIR/$f" ]; then
    cp "$PRESERVE_DIR/$f" "$PERM_DIR/$f"
    [ "$f" = "config.json" ] && chmod 600 "$PERM_DIR/$f" 2>/dev/null || true
  fi
done
for d in "${PRESERVE_DIRS[@]}"; do
  [ -d "$PRESERVE_DIR/$d" ] && cp -R "$PRESERVE_DIR/$d" "$PERM_DIR/$d"
done

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

# --- 同步依赖（uv sync 会按 uv.lock 精确安装，含 Python 3.13）---
echo "安装依赖（首次可能需要下载 Python 3.13，请稍候）..."
cd "$PERM_DIR"
uv sync --quiet 2>&1 | grep -v "^$" || true

# --- 启动服务 ---
# 后台启动 uvicorn，输出重定向到日志文件；端口被占用时给出提示。
LOG_FILE="$PERM_DIR/quotax.log"
echo "启动 QuotaX（端口 $PORT）..."

# 若已有旧进程占用端口，先尝试优雅结束。
if command -v lsof &>/dev/null; then
  OLD_PID=$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$OLD_PID" ]; then
    echo "端口 $PORT 被占用（PID $OLD_PID），正在结束旧进程..."
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
  fi
fi

# nohup 后台启动，退出本脚本后服务继续运行。
nohup uv run uvicorn app.main:app --host 127.0.0.1 --port "$PORT" > "$LOG_FILE" 2>&1 &
APP_PID=$!

# 等待服务就绪（最多 30 秒轮询 /health 或 TCP 端口）。
echo -n "等待服务就绪"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/" -o /dev/null 2>/dev/null; then
    echo " ✅"
    READY=1
    break
  fi
  # 进程已退出则提前报错。
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo ""
    echo "Error: 服务启动失败，日志见 $LOG_FILE：" >&2
    tail -20 "$LOG_FILE" >&2 2>/dev/null || true
    exit 1
  fi
  echo -n "."
  sleep 1
done
echo ""

# --- 尝试打开浏览器 ---
if [ -z "${READY:-}" ]; then
  echo "⚠️  服务仍在启动中（超过 30 秒），稍后访问 http://127.0.0.1:$PORT"
  echo "   日志: $LOG_FILE"
else
  # macOS 用 open，Linux 试 xdg-open。
  (command -v open &>/dev/null && open "http://127.0.0.1:$PORT") \
    || (command -v xdg-open &>/dev/null && xdg-open "http://127.0.0.1:$PORT") \
    || true
fi

echo ""
echo "============================================="
echo "✅ QuotaX 已启动"
echo "   地址:  http://127.0.0.1:$PORT"
echo "   日志:  $LOG_FILE"
echo "   目录:  $PERM_DIR"
echo ""
echo "   停止:        lsof -ti tcp:$PORT | xargs kill"
echo "   再次启动:    cd $PERM_DIR && uv run uvicorn app.main:app --port $PORT"
echo "============================================="
