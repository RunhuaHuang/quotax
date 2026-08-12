"""usage.pricing 单测：归一化 fallback、成本计算、LiteLLM 合并。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.usage import pricing


def test_fallback_candidates_strips_date_suffix():
    """版本号 + 日期后缀应被 fallback 候选逐段去掉。"""
    cands = pricing.fallback_candidates("claude-sonnet-5-20260101")
    assert "claude-sonnet-5" in cands
    assert "claude-sonnet" in cands
    assert "claude" in cands


def test_fallback_candidates_provider_prefix():
    cands = pricing.fallback_candidates("anthropic/claude-3-5-sonnet")
    assert cands[0] == "claude-3-5-sonnet"


def test_resolve_known_model():
    book = pricing.PricingBook()
    rate = book.resolve("claude-sonnet-5")
    assert rate.input == Decimal("2.0")  # 优惠期至 2026-08-31
    assert rate.output == Decimal("10.0")
    assert not rate.is_unknown()


def test_resolve_via_fallback():
    """带日期后缀的模型应通过 fallback 命中内置价。"""
    book = pricing.PricingBook()
    rate = book.resolve("claude-sonnet-5-20260101")
    assert rate.input == Decimal("2.0")


def test_resolve_unknown_model_returns_none():
    book = pricing.PricingBook()
    rate = book.resolve("totally-fake-model-xyz")
    assert rate.is_unknown()


def test_calc_cost_four_buckets():
    book = pricing.PricingBook()
    rate = book.resolve("claude-sonnet-5")  # 优惠期 $2/$10/$0.2/$2.5
    cost = pricing.calc_cost(
        {"input": 1_000_000, "output": 500_000, "cache_read": 200_000, "cache_creation": 100_000},
        rate,
    )
    # 2.0/1M*1M=2.0；10/1M*0.5M=5.0；0.2/1M*0.2M=0.04；2.5/1M*0.1M=0.25
    assert cost["input_usd"] == Decimal("2.000000")
    assert cost["output_usd"] == Decimal("5.000000")
    assert cost["total_usd"] == Decimal("7.290000")


def test_calc_cost_unknown_model_zero():
    book = pricing.PricingBook()
    rate = book.resolve("fake-model")
    cost = pricing.calc_cost({"input": 1000, "output": 500, "cache_read": 0, "cache_creation": 0}, rate)
    assert cost["total_usd"] == Decimal(0)


def test_calc_cost_ignores_nonfinite_and_negative_token_counts():
    book = pricing.PricingBook()
    rate = book.resolve("claude-sonnet-5")
    cost = pricing.calc_cost(
        {"input": float("nan"), "output": float("inf"), "cache_read": -1, "cache_creation": "not-a-number"},
        rate,
    )
    assert cost["total_usd"] == Decimal("0.000000")


def test_upsert_overrides_builtin():
    book = pricing.PricingBook()
    book.upsert("claude-sonnet-5", 99.0, 99.0, 0, 0, is_builtin=False)
    rate = book.resolve("claude-sonnet-5")
    assert rate.input == Decimal("99.0")
    entry = next(e for e in book.entries() if e["model_key"] == "claude-sonnet-5")
    assert entry["is_builtin"] is True


@pytest.mark.parametrize("invalid", [-1, "NaN", "Infinity", 1_000_001, True])
def test_upsert_rejects_invalid_rates(invalid):
    book = pricing.PricingBook()
    with pytest.raises(ValueError):
        book.upsert("custom-model", invalid, 0, 0, 0)


def test_builtin_prices_include_current_cache_write_and_gemini_rates():
    book = pricing.PricingBook()
    gpt = book.resolve("gpt-5.6-terra")
    assert gpt.cache_creation == Decimal("2.5")

    gemini_pro = book.resolve("gemini-2.5-pro")
    assert gemini_pro.output == Decimal("10.0")

    gemini_flash = book.resolve("gemini-2.5-flash")
    assert gemini_flash.input == Decimal("0.3")
    assert gemini_flash.output == Decimal("2.5")


def test_apply_litellm_merges():
    """apply_litellm 接收的是 fetch_litellm 产出的 per_million 值（已转换）。"""
    book = pricing.PricingBook()
    # fetch_litellm 已把 per_token × 1M，这里模拟它产出 per_million 值
    fetched = {
        "openai/gpt-new": (
            Decimal(2000),  # 0.002 per_token × 1M
            Decimal(6000),
            Decimal(0),
            Decimal(0),
        )
    }
    n = pricing.apply_litellm(book, fetched)
    assert n == 1
    rate = book.resolve("gpt-new")
    assert rate.input == Decimal("2000.0")
