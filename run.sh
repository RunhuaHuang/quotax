#!/bin/zsh
# QuotaX 启动脚本
cd "$(dirname "$0")"
PORT="${QUOTAX_PORT:-8900}"
if ! [[ "$PORT" =~ ^[0-9]{1,5}$ ]]; then
  echo "Error: QUOTAX_PORT 必须是 1 到 65535 之间的整数。" >&2
  exit 2
fi
PORT_NUMBER=$((10#$PORT))
if (( PORT_NUMBER < 1 || PORT_NUMBER > 65535 )); then
  echo "Error: QUOTAX_PORT 必须是 1 到 65535 之间的整数。" >&2
  exit 2
fi
exec uv run uvicorn app.main:app --host 127.0.0.1 --port "$PORT_NUMBER" "$@"
