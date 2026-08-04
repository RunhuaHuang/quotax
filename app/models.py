"""统一的结果模型：所有渠道的额度/余额都归一化到这个结构。"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass
class QuotaWindow:
    """一个额度窗口（5 小时 / 每周 / 每月 / 账户余额等）。"""

    key: str  # five_hour / weekly / monthly / credits / balance / custom
    label: str  # 展示名："每 5 小时" / "每周额度" ...
    used_percent: float | None = None
    remaining_percent: float | None = None
    used_label: str | None = None  # 已用文案："$4.20"
    max_label: str | None = None  # 总额文案："$12"
    reset_at: int | None = None  # epoch 毫秒


@dataclass
class ChannelResult:
    """单个渠道的统一查询结果。"""

    id: str
    type: str
    name: str
    category: str  # balance / coding_plan / subscription / local
    status: str  # ok / info / expired / not_found / error / disabled
    # （取值含义详见 README.md「ChannelResult.status 取值」一节）
    message: str | None = None
    plan_name: str | None = None
    amount: dict | None = None  # {"value": float, "currency": str, "label": str}
    windows: list[QuotaWindow] = field(default_factory=list)
    source: str | None = None  # 凭据来源说明（如 "~/.claude/.credentials.json"）
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))
    # 少数 provider 需要带出比标准字段更细的结构化信息，又不想为每个特例单独加
    # 一个顶层字段（那样这个 dataclass 会无限膨胀）。目前唯一的使用者：火山方舟
    # 渠道用 extra["agent_plan_name"]/["coding_plan_name"] 带出 Agent/Coding 两个
    # 套餐各自的真实名称（含 PlanType 档位，如 "火山 Agent Plan small"），供
    # app/main.py 把渠道拆成两张卡片时各自取用——而不是从 plan_name 拼接后的
    # "火山 Agent Plan small · 火山 Coding Plan" 字符串里反过来解析（main.py 曾经
    # 因为没有这个结构化字段，直接把两张卡的 plan_name 硬编码成通用的
    # "Agent Plan"/"Coding Plan"，丢掉了套餐档位信息）。默认空 dict，其余 provider
    # 不使用时序列化结果里只是一个无害的 {}。
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def window(
    key: str,
    label: str,
    used_percent: float | None = None,
    remaining_percent: float | None = None,
    used_label: str | None = None,
    max_label: str | None = None,
    reset_at: int | None = None,
) -> QuotaWindow:
    return QuotaWindow(
        key=key,
        label=label,
        used_percent=round(used_percent, 1) if used_percent is not None else None,
        remaining_percent=round(remaining_percent, 1) if remaining_percent is not None else None,
        used_label=used_label,
        max_label=max_label,
        reset_at=reset_at,
    )


def amount(value: float, currency: str = "", symbol: str = "") -> dict:
    """金额对象：value 为数值，label 为格式化展示串。"""
    label = f"{symbol}{value:,.2f}" if symbol else f"{value:,.2f} {currency}".strip()
    return {"value": round(value, 2), "currency": currency, "label": label}


def ok(**kwargs) -> ChannelResult:
    return ChannelResult(status="ok", **kwargs)


def fail(status: str, message: str, **kwargs) -> ChannelResult:
    return ChannelResult(status=status, message=message, **kwargs)


def to_ts(value) -> int | None:
    """统一的时间戳归一：兼容秒/毫秒/ISO8601 字符串 → epoch 毫秒。

    各 provider（volcengine / coding_plans / mimo / subscriptions）原本各有一份
    近乎相同的私有 _to_ts/_iso_to_ts，逻辑一致，集中到这里避免四份实现慢慢漂移。
    返回 None 表示无法解析（调用方应保留上游原值或留空）。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and value > 0:
        # < 1e10 视为秒级，否则毫秒级
        return int(value * 1000 if value < 10_000_000_000 else value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp() * 1000)
        except ValueError:
            return None
    return None
