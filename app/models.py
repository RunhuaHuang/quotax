"""统一的结果模型：所有渠道的额度/余额都归一化到这个结构。"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field


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
