"""QuotaX — 命令行工具。

一次性查询各渠道额度摘要（终端 / tmux / shell prompt / 脚本用），以及脚本化
配置渠道密钥、查看本地已用统计。复用与 Web 后端完全相同的查询与配置逻辑，
不发额外请求、不写任何凭据副本。

用法（uv run quotaboard --help / python -m app.cli --help）：

    quotaboard quota [--json | --brief] [--ids ch_a,ch_b]   查询额度摘要
    quotaboard channels [--json]                            列出渠道（密钥打码）
    quotaboard cost [--days N] [--json]                     本地已用统计
    quotaboard config set-api-key --channel <id> --key <k>  脚本化更新渠道 API Key

退出码：0 全部渠道正常（ok/info/disabled）；1 存在 error/expired/not_found
渠道；2 配置损坏或用例错误（argparse 用法错误本身退出 2，与 argparse 约定一致）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time

from . import config as config_store
from . import local_usage, net
from .models import ChannelResult, fail
from .providers import channel_category, query_channel

# 与 Web 端 /api/quotas 的状态语义一致（详见 README「ChannelResult.status 取值」）
_STATUS_SYMBOL = {
    "ok": "✓",
    "info": "ⓘ",
    "expired": "⚠",
    "not_found": "○",
    "error": "✗",
    "disabled": "–",
}
_FAIL_STATUSES = ("error", "expired", "not_found")


def _fmt_reset(ms: int) -> str:
    """重置倒计时文案（与前端 fmtReset 同口径：向上取整，避免显示 0 分钟）。"""
    diff = ms - int(time.time() * 1000)
    if diff <= 0:
        return "已重置"
    if diff < 3600_000:
        return f"{math.ceil(diff / 60_000)} 分钟后"
    if diff < 86400_000:
        return f"{math.ceil(diff / 3600_000)} 小时后"
    return f"{math.ceil(diff / 86400_000)} 天后"


def _window_desc(w: dict) -> str:
    """单个额度窗口的摘要：如「每周额度 剩 31% · 重置 2 小时后」。"""
    parts = []
    if w.get("remaining_percent") is not None:
        parts.append(f"剩 {w['remaining_percent']}%")
    elif w.get("used_percent") is not None:
        parts.append(f"已用 {w['used_percent']}%")
    if w.get("reset_at"):
        parts.append(f"重置 {_fmt_reset(w['reset_at'])}")
    return f"{w.get('label', '')} " + " · ".join(parts) if parts else str(w.get("label", ""))


def _info_text(payload: dict) -> str:
    """单渠道的信息列：ok 显示金额/窗口摘要，其余显示 message 或状态名。"""
    status = payload["status"]
    if status == "ok":
        parts = []
        if payload.get("amount"):
            parts.append(payload["amount"]["label"])
        parts.extend(_window_desc(w) for w in payload.get("windows") or [])
        return " · ".join(parts)
    if status == "disabled":
        return "已停用"
    return payload.get("message") or status


def _brief_segment(payload: dict) -> str:
    """--brief 单行模式下单个渠道的片段：ok 取第一个剩余百分比，其余取状态符号。"""
    name = payload["name"]
    status = payload["status"]
    if status == "ok":
        pct = next(
            (w["remaining_percent"] for w in payload.get("windows") or [] if w.get("remaining_percent") is not None),
            None,
        )
        if pct is not None:
            return f"{name} {pct}%"
        if payload.get("amount"):
            return f"{name} {payload['amount']['label']}"
        return name
    return f"{name} {_STATUS_SYMBOL.get(status, status)}"


def _disabled_payload(channel: config_store.Channel) -> dict:
    return ChannelResult(
        id=channel.id,
        type=channel.type,
        name=channel.name,
        category=channel_category(channel.type),
        status="disabled",
    ).to_dict()


async def _fetch_quotas(ids: set[str] | None) -> list[dict]:
    """并行查询渠道（停用的不发起网络请求），单渠道异常兜底成 error 结果。

    与 Web 端 /api/quotas 同一套容错语义；CLI 是一次性进程，没有进程内缓存，
    也不需要——每次运行都是全新查询。
    """
    channels = config_store.list_channels()
    if ids:
        channels = [c for c in channels if c.id in ids]

    async def one(channel: config_store.Channel) -> dict:
        if not channel.enabled:
            return _disabled_payload(channel)
        try:
            return (await query_channel(channel)).to_dict()
        except Exception as e:  # query_channel 内部已兜底，这里是双重保险
            return fail(
                "error",
                str(e) or e.__class__.__name__,
                id=channel.id,
                type=channel.type,
                name=channel.name,
                category=channel_category(channel.type),
            ).to_dict()

    return list(await asyncio.gather(*(one(c) for c in channels)))


def _exit_code(payloads: list[dict]) -> int:
    return 1 if any(p["status"] in _FAIL_STATUSES for p in payloads) else 0


# ── 子命令实现 ──────────────────────────────────────────────


def _cmd_quota(args: argparse.Namespace) -> int:
    ids = {x.strip() for x in args.ids.split(",") if x.strip()} if args.ids else None

    async def run() -> tuple[list[dict], None]:
        try:
            return await _fetch_quotas(ids), None
        finally:
            await net.aclose()  # 必须留在事件循环内关闭全局 httpx 客户端

    payloads, _ = asyncio.run(run())
    if args.json:
        print(json.dumps({"generated_at": int(time.time() * 1000), "channels": payloads}, ensure_ascii=False, indent=2))
    elif args.brief:
        print(" · ".join(_brief_segment(p) for p in payloads))
    else:
        width = max((len(p["name"]) for p in payloads), default=0)
        for p in payloads:
            symbol = _STATUS_SYMBOL.get(p["status"], p["status"])
            print(f"{p['name']:<{width}}  {symbol}  {_info_text(p)}")
    return _exit_code(payloads)


def _cmd_channels(args: argparse.Namespace) -> int:
    channels = config_store.list_channels()
    if args.json:
        items = []
        for c in channels:
            d = {"id": c.id, "type": c.type, "name": c.name, "enabled": c.enabled}
            secret = c.api_key or c.ak or c.sk
            d["api_key_masked"] = config_store.mask_secret(secret) if secret else None
            items.append(d)
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        for c in channels:
            secret = c.api_key or c.ak or c.sk
            key = config_store.mask_secret(secret) if secret else "-"
            state = "on" if c.enabled else "off"
            print(f"{c.id}\t{c.type}\t{c.name}\t{state}\t{key}")
    return 0


def _cmd_cost(args: argparse.Namespace) -> int:
    data = asyncio.run(asyncio.to_thread(local_usage.get_local_usage, args.days))
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    for s in data.get("sources", []):
        label = s.get("label", s.get("key", "?"))
        if not s.get("available"):
            print(f"{label}: 不可用（{s.get('message') or '未知原因'}）")
            continue
        totals = s.get("totals") or {}
        parts = [f"{totals.get('sessions', 0)} 会话", f"{totals.get('messages', 0)} 消息"]
        for k in ("input", "output", "cache_read", "cache_write"):
            if totals.get(k):
                parts.append(f"{k} {totals[k]}")
        if totals.get("has_cost"):
            parts.append(f"费用 {totals.get('cost', 0)}")
        else:
            parts.append("无费用数据")
        print(f"{label}（近 {data.get('days', args.days)} 天）: " + " · ".join(parts))
    return 0


def _cmd_set_api_key(args: argparse.Namespace) -> int:
    channel = config_store.get_channel(args.channel)
    if channel is None:
        print(f"错误: 渠道不存在: {args.channel}（用 `quotaboard channels` 查看渠道 id）", file=sys.stderr)
        return 1
    fields = config_store.PROVIDERS.get(channel.type, {}).get("fields", [])
    if "api_key" not in fields:
        print(
            f"错误: 渠道类型 {channel.type} 不存储 API Key（该类型需要的字段: {fields or '无'}）",
            file=sys.stderr,
        )
        return 1
    # provided_fields 只含 id/type/api_key：其余字段（name/base_url/enabled 等）
    # 沿用旧值，与 Web 端"最小 payload 不覆盖其它字段"的语义完全一致。
    config_store.upsert_channel(
        {"id": channel.id, "type": channel.type, "api_key": args.key},
        provided_fields={"id", "type", "api_key"},
    )
    print(f"已更新 {channel.name} ({channel.id}) 的 API Key: {config_store.mask_secret(args.key)}")
    return 0


# ── 入口 ────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quotaboard",
        description="QuotaX 命令行工具：一次性查询各渠道额度摘要、查看本地已用统计、脚本化配置渠道。",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="子命令")

    p_quota = sub.add_parser("quota", help="查询各渠道额度摘要（默认文本分栏，可 --json / --brief）")
    mode = p_quota.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="输出结构化 JSON（供脚本 / jq 使用）")
    mode.add_argument("--brief", action="store_true", help="单行紧凑摘要（tmux statusbar / shell prompt 用）")
    p_quota.add_argument("--ids", metavar="ch_a,ch_b", help="只查询指定渠道 id（逗号分隔，不传则查全部）")
    p_quota.set_defaults(func=_cmd_quota)

    p_channels = sub.add_parser("channels", help="列出渠道（id / 类型 / 名称 / 启用状态 / 密钥打码）")
    p_channels.add_argument("--json", action="store_true", help="输出 JSON")
    p_channels.set_defaults(func=_cmd_channels)

    p_cost = sub.add_parser("cost", help="本地已用统计（Claude Code transcript / OpenCode，只读本机文件）")
    p_cost.add_argument("--days", type=int, default=14, help="统计最近 N 天（1-90，默认 14）")
    p_cost.add_argument("--json", action="store_true", help="输出 JSON")
    p_cost.set_defaults(func=_cmd_cost)

    p_config = sub.add_parser("config", help="配置操作")
    p_config_sub = p_config.add_subparsers(dest="config_command", required=True, metavar="config 子命令")
    p_setkey = p_config_sub.add_parser("set-api-key", help="更新某渠道的 API Key（渠道须已存在且类型支持 api_key）")
    p_setkey.add_argument("--channel", required=True, help="渠道 id（用 `quotaboard channels` 查看）")
    p_setkey.add_argument("--key", required=True, help="新的 API Key")
    p_setkey.set_defaults(func=_cmd_set_api_key)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except config_store.ConfigCorruptedError as e:
        # 与 Web 端一致：配置损坏绝不能假装"没有渠道"
        print(f"错误: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
