#!/usr/bin/env bash
# QuotaX 启动脚本
cd "$(dirname "$0")"
PORT="${QUOTAX_PORT:-8900}"
HOST="${QUOTAX_HOST:-127.0.0.1}"
if ! [[ "$PORT" =~ ^[0-9]{1,5}$ ]]; then
  echo "Error: QUOTAX_PORT 必须是 1 到 65535 之间的整数。" >&2
  exit 2
fi
PORT_NUMBER=$((10#$PORT))
if (( PORT_NUMBER < 1 || PORT_NUMBER > 65535 )); then
  echo "Error: QUOTAX_PORT 必须是 1 到 65535 之间的整数。" >&2
  exit 2
fi
# HOST 进入 uvicorn 参数，限制为 IP/主机名合法字符（含 IPv6 字面量方括号）。
# 不用 [[ =~ ]]：macOS 系统自带 bash 3.2 对含 \[ 的字面量正则解析不可靠（实测
# 全部 NOMATCH），改用 glob（case）匹配，所有 bash 版本行为一致。
case "$HOST" in
  [\[]*[\]) HOST_INNER="${HOST:1:${#HOST}-2}" ;;  # [::1] → ::1
  *) HOST_INNER="$HOST" ;;
esac
case "$HOST_INNER" in
  ""|*[!A-Za-z0-9.:-]*)
    echo "Error: QUOTAX_HOST 只能是 IP 地址或主机名（当前值: $HOST）。" >&2
    exit 2
    ;;
esac
exec uv run uvicorn app.main:app --host "$HOST" --port "$PORT_NUMBER" "$@"
