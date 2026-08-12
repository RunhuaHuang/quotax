"""统一结果模型的边界数值回归测试。"""

from __future__ import annotations

import json
import math

from app.models import amount, to_ts, window


def test_window_drops_non_finite_percentages_and_clamps_valid_values():
    result = window(
        "quota",
        "额度",
        used_percent=float("nan"),
        remaining_percent=float("inf"),
    )
    assert result.used_percent is None
    assert result.remaining_percent is None

    bounded = window("quota", "额度", used_percent=120, remaining_percent=-5)
    assert bounded.used_percent == 100.0
    assert bounded.remaining_percent == 0.0
    json.dumps(bounded.__dict__, allow_nan=False)


def test_amount_and_timestamp_reject_non_finite_values():
    safe_amount = amount(float("inf"), "USD", "$")
    assert safe_amount == {"value": 0.0, "currency": "USD", "label": "$0.00"}
    assert to_ts(float("nan")) is None
    assert to_ts(float("inf")) is None
    assert to_ts(True) is None
    assert math.isfinite(safe_amount["value"])


def test_to_ts_accepts_numeric_strings_and_treats_naive_iso_as_utc():
    assert to_ts("1722507600") == 1722507600000
    assert to_ts("1722507600000") == 1722507600000
    assert to_ts("2026-08-01T10:00:00Z") == 1785578400000
    assert to_ts("2026-08-01T10:00:00") == 1785578400000
