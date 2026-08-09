"""模型单价表与成本计算。

参考 Buktal/VaultOne 的 pricing.rs 设计：
- 四桶分离计费：input / output / cache_read / cache_creation，单价单位是
  "美元 / 百万 token"，计算时 tokens × rate / 1_000_000；
- 模型名归一化 fallback 查找：`claude-sonnet-5-20260101` 先试完整名，再按
  `-` 逐段去掉尾部（`claude-sonnet-5` → `claude-sonnet` → `claude`），命中即用，
  对版本号 / 日期后缀非常实用；
- LiteLLM 拉取：从 BerriAI/litellm 仓库的 model_prices_and_context_window.json
  取全网模型单价，per_token → per_million 转换，过滤伪条目；
- 成本用 decimal.Decimal 计算，避免浮点累计误差（token 量级大，float 累加会出错）；
- rebill：只补当初无价记为 0 的记录，不重写已有价格的记录（保证历史稳定可审计）。

内置种子价格仅作离线兜底，实际应以 LiteLLM 拉取为准（会覆盖同 key 的内置价）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# LiteLLM 全网模型单价表（BerriAI 维护，社区常用）。原始字段是 per_token，
# 这里会乘 1_000_000 转成 per_million，与内置价格口径一致。
LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# 内置种子价格（USD / 1M tokens），顺序 input / output / cache_read / cache_creation。
# 仅作离线兜底 + LiteLLM 拉取失败时的保底；其中 0 表示该桶未知 / 不计费。
# 数据来源：Anthropic / OpenAI / DeepSeek / Google / xAI 官网定价页（2026-08）。
#
# 重要：归一化 fallback 按 `-` 逐段去尾，所以每个代际的精确名都要单独建条目，
# 否则会误 fallback 到错误价位。例如 claude-opus-4-8 若无独立条目会落到
# claude-opus-4（$15/$75），但 opus 4.5~4.8 实际已降到 $5/$25——必须各自建条目。
BUILTIN_PRICES: dict[str, tuple[float, float, float, float]] = {
    # ── Anthropic Claude（platform.claude.com/docs/en/about-claude/pricing）──
    # 缓存读 = 标准输入的 10%；缓存写（5min）= 标准输入的 1.25 倍。
    # opus 5 / 4.8 / 4.7 / 4.6 / 4.5 同价 $5/$25；opus 4.1 / 4 是旧价 $15/$75
    "claude-fable-5": (10.0, 50.0, 1.0, 12.5),
    "claude-opus-5": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.5, 6.25),  # 含 -thinking 变体
    "claude-opus-4-7": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-6": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-5": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-1": (15.0, 75.0, 1.5, 18.75),
    "claude-opus-4": (15.0, 75.0, 1.5, 18.75),
    "claude-3-opus": (15.0, 75.0, 1.5, 18.75),
    "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5),  # 优惠期至 2026-08-31，之后 $3/$15
    "claude-sonnet-4-6": (3.0, 15.0, 0.3, 3.75),
    "claude-sonnet-4-5": (3.0, 15.0, 0.3, 3.75),
    "claude-sonnet-4": (3.0, 15.0, 0.3, 3.75),
    "claude-3-5-sonnet": (3.0, 15.0, 0.3, 3.75),
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25),
    "claude-haiku-4": (1.0, 5.0, 0.1, 1.25),
    "claude-3-5-haiku": (0.8, 4.0, 0.08, 1.0),
    # ── OpenAI GPT（developers.openai.com/api/docs/pricing）──
    # cached input = input 的 10%。5.6 系列分 sol/terra/luna 三档不同价。
    "gpt-5.6-sol": (5.0, 30.0, 0.5, 0.0),
    "gpt-5.6-terra": (2.0, 12.0, 0.2, 0.0),
    "gpt-5.6-luna": (0.2, 1.2, 0.02, 0.0),
    "gpt-5.6": (2.0, 12.0, 0.2, 0.0),  # 无后缀的 5.6 按 terra 档兜底
    "gpt-5.5": (5.0, 30.0, 0.5, 0.0),
    "gpt-5.5-pro": (30.0, 180.0, 0.0, 0.0),
    "gpt-5.4": (2.5, 15.0, 0.25, 0.0),
    "gpt-5.4-mini": (0.75, 4.5, 0.075, 0.0),
    "gpt-5.4-nano": (0.2, 1.25, 0.02, 0.0),
    "gpt-5.3-codex": (1.75, 14.0, 0.175, 0.0),
    "gpt-5.3-chat-latest": (1.75, 14.0, 0.175, 0.0),
    "gpt-5.2": (1.75, 14.0, 0.175, 0.0),
    "gpt-5.1": (1.25, 10.0, 0.125, 0.0),
    "gpt-5": (1.25, 10.0, 0.125, 0.0),
    "gpt-5-mini": (0.25, 2.0, 0.025, 0.0),
    "gpt-5-nano": (0.05, 0.4, 0.005, 0.0),
    "gpt-5-pro": (15.0, 120.0, 0.0, 0.0),
    "gpt-4.1": (2.0, 8.0, 0.5, 0.0),
    "gpt-4.1-mini": (0.4, 1.6, 0.1, 0.0),
    "gpt-4.1-nano": (0.1, 0.4, 0.025, 0.0),
    "gpt-4o": (2.5, 10.0, 1.25, 2.5),
    "gpt-4o-mini": (0.15, 0.6, 0.075, 0.15),
    "gpt-4-turbo": (10.0, 30.0, 5.0, 0.0),
    # ── Google Gemini ──
    "gemini-2.5-pro": (1.25, 5.0, 0.125, 1.25),
    "gemini-2.5-flash": (0.075, 0.3, 0.01875, 0.075),
    "gemini-2.0-flash": (0.075, 0.3, 0.01875, 0.075),
    # ── DeepSeek（api-docs.deepseek.com/quick_start/pricing）──
    # cache hit input = cache miss 的 2%。v4-flash / v4-pro 两个档。
    "deepseek-v4-flash": (0.14, 0.28, 0.0028, 0.0),
    "deepseek-v4-pro": (0.435, 0.87, 0.003625, 0.0),
    "deepseek-chat": (0.14, 0.28, 0.0028, 0.0),  # chat 通常对应 flash 档
    "deepseek-reasoner": (0.435, 0.87, 0.003625, 0.0),  # reasoner 对应 pro 档
    # ── 智谱 GLM ──
    "glm-4.6": (0.6, 2.2, 0.06, 0.75),
    "glm-4.5": (0.6, 2.2, 0.06, 0.75),
    # ── xAI Grok ──
    "grok-4": (3.0, 15.0, 0.3, 3.75),
    "grok-4-fast": (0.2, 1.5, 0.02, 0.25),
    # ── Kimi / MiniMax / 等（订阅常见，补几个兜底）──
    "kimi": (0.24, 0.96, 0.024, 0.0),
    "minimax": (1.0, 1.0, 0.0, 0.0),
}

_MILLION = Decimal(1000000)
_PER_MILLION_QUANTUM = Decimal("0.000001")  # 单价精度：6 位小数（USD/1M）


@dataclass(frozen=True)
class ResolvedRate:
    """已归一化的四桶单价（USD / 1M tokens）。任一桶为 None 表示无价（不计费）。"""

    input: Decimal | None
    output: Decimal | None
    cache_read: Decimal | None
    cache_creation: Decimal | None

    def is_unknown(self) -> bool:
        return all(v is None for v in (self.input, self.output, self.cache_read, self.cache_creation))


def _to_decimal(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def normalize_key(model: str) -> str:
    """模型名归一化：小写 + 去掉常见后缀噪音，用于定价表匹配。

    例：`claude-sonnet-5-20260101` → `claude-sonnet-5-20260101`（先试完整），
    fallback 阶段再逐段去尾部。这里只做基础清理：小写、去空白和 provider 前缀
    （如 `anthropic/claude-...` → `claude-...`），保留版本号以便完整名优先命中。
    """
    if not model:
        return ""
    s = str(model).strip().lower()
    # 去掉 provider/ 前缀（litellm 的 key 形如 "anthropic/claude-3-5-sonnet"）
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    # 去掉日期后缀的硬编码模式不算在这里——留给 fallback 候选生成处理
    return s


def fallback_candidates(model: str) -> list[str]:
    """生成归一化键的多级 fallback 候选，依次试到命中。

    `claude-sonnet-5-20260101` →
        ['claude-sonnet-5-20260101', 'claude-sonnet-5', 'claude-sonnet', 'claude']
    先去掉纯日期后缀段（8 位数字），再按 `-` 逐段去尾。这与 VaultOne 的
    normalization_candidates 思路一致。
    """
    key = normalize_key(model)
    if not key:
        return []
    # 先剥掉末尾的日期段：-20260101 / -2025-10-22 这种
    key = re.sub(r"-\d{6,8}(?:-\d{2})?$", "", key)
    parts = key.split("-")
    candidates: list[str] = []
    seen: set[str] = set()
    # 从完整名开始，逐段去尾
    for i in range(len(parts), 0, -1):
        cand = "-".join(parts[:i])
        if cand and cand not in seen:
            seen.add(cand)
            candidates.append(cand)
    return candidates


class PricingBook:
    """单价表：model_key → 四桶单价（USD / 1M tokens）。

    内置价标记 is_builtin=True，LiteLLM 拉取 / 手动编辑的覆盖项 is_builtin=False。
    查找时先精确匹配，再走 fallback 候选；都未命中返回全 None（未知模型）。
    """

    def __init__(self) -> None:
        # key: normalized model key → (input, output, cache_read, cache_creation, is_builtin)
        self._by_key: dict[str, tuple[Decimal, Decimal, Decimal, Decimal, bool]] = {}
        self._load_builtin()

    def _load_builtin(self) -> None:
        for key, (inp, out, cr, cc) in BUILTIN_PRICES.items():
            self._by_key[normalize_key(key)] = (
                _to_decimal(inp),
                _to_decimal(out),
                _to_decimal(cr),
                _to_decimal(cc),
                True,
            )

    def resolve(self, model: str) -> ResolvedRate:
        """按归一化 + fallback 查找模型单价。未命中返回全 None。"""
        for cand in fallback_candidates(model):
            entry = self._by_key.get(cand)
            if entry is not None:
                return ResolvedRate(entry[0], entry[1], entry[2], entry[3])
        return ResolvedRate(None, None, None, None)

    def has(self, model: str) -> bool:
        return not self.resolve(model).is_unknown()

    def upsert(
        self,
        model_key: str,
        input_: float | Decimal | None,
        output: float | Decimal | None,
        cache_read: float | Decimal | None,
        cache_creation: float | Decimal | None,
        is_builtin: bool = False,
    ) -> None:
        """新增 / 覆盖一个模型单价。None 的桶按 0（不计费）存。

        手动编辑和 LiteLLM 拉取都用这个入口；is_builtin=False 表示用户 / 远端来源，
        查询时和内置价一视同仁（resolve 不区分来源）。
        """
        key = normalize_key(model_key)
        if not key:
            return
        self._by_key[key] = (
            _to_decimal(input_ or 0),
            _to_decimal(output or 0),
            _to_decimal(cache_read or 0),
            _to_decimal(cache_creation or 0),
            is_builtin,
        )

    def remove(self, model_key: str) -> bool:
        return self._by_key.pop(normalize_key(model_key), None) is not None

    def entries(self) -> list[dict]:
        """导出全部单价项（按 key 排序），供前端定价表展示 / 编辑。"""
        out: list[dict] = []
        for key in sorted(self._by_key):
            inp, outp, cr, cc, builtin = self._by_key[key]
            out.append(
                {
                    "model_key": key,
                    "input_per_million": float(inp),
                    "output_per_million": float(outp),
                    "cache_read_per_million": float(cr),
                    "cache_creation_per_million": float(cc),
                    "is_builtin": builtin,
                }
            )
        return out

    def __len__(self) -> int:
        return len(self._by_key)


def calc_cost(
    tokens: dict, rate: ResolvedRate
) -> dict[str, Decimal]:
    """按四桶单价计算成本。tokens 是 {input, output, cache_read, cache_creation} 整数。

    返回 {input_usd, output_usd, cache_read_usd, cache_creation_usd, total_usd}，
    全 Decimal。未知模型（rate.is_unknown）返回全 0——成本记 0 但 has_cost=False
    会在上层标记，让 rebill 能事后补上。
    """
    zero = Decimal(0)

    def bucket(field: str, price: Decimal | None) -> Decimal:
        if price is None:
            return zero
        n = tokens.get(field, 0) or 0
        return (Decimal(n) * price / _MILLION).quantize(_PER_MILLION_QUANTUM, rounding=ROUND_HALF_UP)

    inp = bucket("input", rate.input)
    out = bucket("output", rate.output)
    cr = bucket("cache_read", rate.cache_read)
    cc = bucket("cache_creation", rate.cache_creation)
    return {
        "input_usd": inp,
        "output_usd": out,
        "cache_read_usd": cr,
        "cache_creation_usd": cc,
        "total_usd": (inp + out + cr + cc).quantize(_PER_MILLION_QUANTUM, rounding=ROUND_HALF_UP),
    }


def fetch_litellm(timeout: float = 15.0) -> dict:
    """从 LiteLLM 仓库拉取全网模型单价，返回 {model_key: (in, out, cache_read, cache_creation)}。

    LiteLLM 原始是 per_token，这里乘 1_000_000 转 per_million，与内置价口径一致。
    过滤 input cost ≤ 0 的伪条目（有些占位项没真实价格）。去掉 `provider/` 前缀。
    返回值里单价都是 Decimal。

    网络失败时抛 URLError，调用方应捕获并保留现有价表不变。
    """
    req = Request(LITELLM_URL, headers={"User-Agent": "QuotaX/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        return {}
    out: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    for name, info in data.items():
        if not isinstance(info, dict):
            continue
        # litellm 顶层混了一些非模型元数据（如 "sample_spec"），只取带价格的
        inp_raw = info.get("input_cost_per_token")
        if inp_raw is None or float(inp_raw) <= 0:
            continue
        key = normalize_key(name)
        if not key:
            continue
        out[key] = (
            _to_decimal(inp_raw) * _MILLION,
            _to_decimal(info.get("output_cost_per_token") or 0) * _MILLION,
            _to_decimal(info.get("cache_read_input_token_cost") or 0) * _MILLION,
            _to_decimal(info.get("cache_creation_input_token_cost") or 0) * _MILLION,
        )
    return out


def apply_litellm(book: PricingBook, fetched: dict) -> int:
    """把 LiteLLM 拉取的单价合并进 PricingBook，返回更新条数。

    只覆盖同 key 的条目；不会删除内置价或手动添加的价。新模型直接 upsert，
    已存在的按远端价覆盖（LiteLLM 是权威来源）。
    """
    count = 0
    for key, (inp, out, cr, cc) in fetched.items():
        book.upsert(
            key,
            float(inp),
            float(out),
            float(cr),
            float(cc),
            is_builtin=False,
        )
        count += 1
    return count


def update_from_litellm(book: PricingBook, timeout: float = 15.0) -> dict:
    """拉取 LiteLLM 并合并进 book，返回 {updated, error}。

    网络失败时 error 给出中文原因，book 保持不变。
    """
    try:
        fetched = fetch_litellm(timeout=timeout)
    except (URLError, OSError, TimeoutError, ValueError) as e:
        return {"updated": 0, "error": f"拉取 LiteLLM 失败: {e}"}
    updated = apply_litellm(book, fetched)
    return {"updated": updated, "error": None}
