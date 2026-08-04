#!/bin/zsh
# QuotaX 启动脚本
cd "$(dirname "$0")"
exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8900 "$@"
