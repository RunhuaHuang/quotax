"""渠道配置存储：config.json 保存在项目根目录，权限 600。

只有 API Key / 火山 AK/SK / 中转站地址这类需要用户手动填写的敏感信息
才会存进这里；订阅类渠道（Claude / Gemini / Grok / Codex / Copilot）
一律直接读取各 CLI 自己的凭据文件，本项目不复制、不写入任何凭据。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 配置文件路径：默认项目根目录的 config.json；可用环境变量 QUOTABOARD_CONFIG
# 覆盖（主要用于本地自测/集成测试时指向临时目录，绝不touched 用户真实配置）。
# pytest 测试请直接 monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "x.json")，
# 不必依赖这个环境变量——模块里的函数都是运行时才读 CONFIG_PATH 这个全局名字，
# 猴子补丁改了之后立刻生效。
CONFIG_PATH = (
    Path(os.environ["QUOTABOARD_CONFIG"])
    if os.environ.get("QUOTABOARD_CONFIG")
    else Path(__file__).resolve().parent.parent / "config.json"
)

# 历史趋势数据目录：与 config.json 同目录下的 history/ 子目录，存 JSONL 趋势记录。
# 用 QUOTABOARD_CONFIG 派生（而不是单独环境变量）——这样测试时只要指了 CONFIG_PATH，
# 历史记录也自动写到同一个 tmp_path 下，不会污染用户真实数据。
_HISTORY_DIR_OVERRIDE = os.environ.get("QUOTABOARD_HISTORY_DIR")
if _HISTORY_DIR_OVERRIDE:
    HISTORY_DIR = Path(_HISTORY_DIR_OVERRIDE)
else:
    HISTORY_DIR = CONFIG_PATH.parent / "history"


class ConfigCorruptedError(RuntimeError):
    """config.json 内容损坏（非合法 JSON 或读取失败），已将原文件备份，不能假装配置为空。"""


# 这些字段名即便出现在某个渠道的 fields 列表里也永远是可选的（新建渠道时不强制必填）。
# api_key / ak / sk / base_url 才是各渠道自己 fields 列表里出现时的必填项。
OPTIONAL_FIELD_NAMES = {"region", "organization", "project", "user_id"}

# 渠道类型目录（含各类所需的配置字段、分类、默认名称）
PROVIDERS: dict[str, dict] = {
    # ── 余额类（API Key 直查）──
    "deepseek": {
        "category": "balance",
        "label": "DeepSeek",
        "fields": ["api_key"],
        "default_name": "DeepSeek 账户余额",
        "manage_url": "https://platform.deepseek.com/usage",
    },
    "stepfun": {
        "category": "balance",
        "label": "阶跃星辰 StepFun",
        "fields": ["api_key"],
        "default_name": "阶跃星辰 余额",
        "manage_url": "https://platform.stepfun.com/",
    },
    "siliconflow": {
        "category": "balance",
        "label": "硅基流动 SiliconFlow",
        "fields": ["api_key"],
        "default_name": "硅基流动 余额",
        "manage_url": "https://cloud.siliconflow.cn/account/balance",
    },
    "openrouter": {
        "category": "balance",
        "label": "OpenRouter",
        "fields": ["api_key"],
        "default_name": "OpenRouter Credits",
        "manage_url": "https://openrouter.ai/settings/credits",
    },
    "novita": {
        "category": "balance",
        "label": "Novita",
        "fields": ["api_key"],
        "default_name": "Novita 余额",
        "manage_url": "https://novita.ai/dashboard/credits",
    },
    "kimi_api": {
        "category": "balance",
        "label": "Kimi API (Moonshot)",
        "fields": ["api_key", "base_url"],
        "default_name": "Kimi API 余额",
        "manage_url": "https://platform.moonshot.cn/console/balance",
    },
    "newapi": {
        "category": "balance",
        "label": "new-api / one-api 中转站",
        "fields": ["api_key", "base_url", "user_id"],
        "default_name": "中转站额度",
    },
    # ── Coding Plan 类 ──
    "kimi_coding": {
        "category": "coding_plan",
        "label": "Kimi For Coding",
        "fields": ["api_key"],
        "default_name": "Kimi For Coding",
        "manage_url": "https://www.kimi.com/code/console?from=kfc_overview_topbar",
    },
    "zhipu_coding": {
        "category": "coding_plan",
        "label": "智谱 GLM Coding Plan",
        "fields": ["api_key"],
        "default_name": "GLM Coding Plan",
        "manage_url": "https://bigmodel.cn/",
    },
    "zhipu_team": {
        "category": "coding_plan",
        "label": "智谱 GLM Coding 团队版",
        "fields": ["api_key", "organization", "project"],
        "default_name": "GLM Coding Plan 团队",
        "manage_url": "https://bigmodel.cn/",
    },
    "minimax": {
        "category": "coding_plan",
        "label": "MiniMax Token Plan",
        "fields": ["api_key"],
        "default_name": "MiniMax Token Plan",
        "manage_url": "https://platform.minimaxi.com/console/personal-info",
    },
    "volcengine": {
        "category": "coding_plan",
        "label": "火山方舟 Agent/Coding Plan",
        "fields": ["ak", "sk", "region"],
        "default_name": "火山方舟",
        "manage_url": "https://console.volcengine.com/ark/",
    },
    "zenmux": {
        "category": "coding_plan",
        "label": "ZenMux",
        "fields": ["api_key", "base_url"],
        "default_name": "ZenMux",
    },
    "mimo": {
        "category": "coding_plan",
        "label": "小米 MiMo Coding Plan",
        "fields": ["api_key"],
        "default_name": "MiMo Coding Plan",
        "manage_url": "https://platform.xiaomimimo.com/",
    },
    # ── 订阅只读类（自动读 CLI 凭据文件）──
    "claude_subscription": {
        "category": "subscription",
        "label": "Claude Pro / Max 订阅",
        "fields": [],
        "default_name": "Claude 订阅",
        "manage_url": "https://claude.ai/settings/billing",
    },
    "gemini_subscription": {
        "category": "subscription",
        "label": "Gemini (AI Studio) 订阅",
        "fields": [],
        "default_name": "Gemini 订阅",
        "manage_url": "https://aistudio.google.com/",
    },
    "grok_subscription": {
        "category": "subscription",
        "label": "Grok (SuperGrok/X) 订阅",
        "fields": [],
        "default_name": "Grok 订阅",
        "manage_url": "https://grok.com/",
    },
    "codex_subscription": {
        "category": "subscription",
        "label": "ChatGPT (Codex) 订阅",
        "fields": [],
        "default_name": "ChatGPT Codex 订阅",
        "manage_url": "https://chatgpt.com/",
    },
    "copilot_subscription": {
        "category": "subscription",
        "label": "GitHub Copilot",
        "fields": [],
        "default_name": "GitHub Copilot",
        "manage_url": "https://github.com/settings/copilot",
    },
}

CATEGORY_LABELS = {
    "balance": "余额（API Key）",
    "coding_plan": "Coding Plan 额度",
    "subscription": "订阅用量（只读凭据）",
    "local": "本地统计",
}


@dataclass
class Channel:
    id: str
    type: str
    name: str
    api_key: str | None = None
    base_url: str | None = None
    ak: str | None = None
    sk: str | None = None
    region: str | None = None
    organization: str | None = None
    project: str | None = None
    user_id: str | None = None  # new-api/one-api 部分部署需要 New-API-User 头配合系统访问令牌
    enabled: bool = True
    extra: dict = field(default_factory=dict)

    def to_dict(self, secret: bool = False) -> dict:
        d = {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "enabled": self.enabled,
        }
        if self.base_url:
            d["base_url"] = self.base_url
        if self.region:
            d["region"] = self.region
        if self.organization:
            d["organization"] = self.organization
        if self.project:
            d["project"] = self.project
        if self.user_id:
            d["user_id"] = self.user_id
        if self.extra:
            d["extra"] = self.extra
        if secret:
            for k in ("api_key", "ak", "sk"):
                v = getattr(self, k)
                if v:
                    d[k] = v
        else:
            for k in ("api_key", "ak", "sk"):
                v = getattr(self, k)
                if v:
                    d[k] = mask_secret(v)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Channel:
        return cls(
            id=str(d.get("id") or f"ch_{uuid.uuid4().hex[:10]}"),
            type=str(d["type"]),
            name=str(d.get("name") or PROVIDERS.get(d["type"], {}).get("default_name", d["type"])),
            api_key=d.get("api_key"),
            base_url=d.get("base_url"),
            ak=d.get("ak"),
            sk=d.get("sk"),
            region=d.get("region"),
            organization=d.get("organization"),
            project=d.get("project"),
            user_id=d.get("user_id"),
            enabled=bool(d.get("enabled", True)),
            extra=d.get("extra") or {},
        )


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * 8 + value[-4:]


def is_masked_secret(value: str | None) -> bool:
    """判断 value 是否形似 mask_secret() 打码后的值。

    前端 GET /api/channels 拿到的密钥是打码串（如 "sk-R********1234"），编辑表单
    如果原样回填、用户没有改动，POST 回来的还是这个打码串——不是真实密钥。这个
    函数用于识别这种情况，配合 upsert_channel 里"打码值一律保留旧值、绝不写入"
    的保护，防止真实密钥被打码串覆盖丢失。
    """
    if not value:
        return False
    if len(value) <= 8:
        return all(ch == "*" for ch in value)
    return len(value) == 16 and value[4:12] == "*" * 8


def _load_raw() -> dict:
    if not CONFIG_PATH.exists():
        return {"channels": []}
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigCorruptedError(f"配置文件读取失败: {e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        # 绝不能假装配置为空——那样上层一保存就会用空配置原地覆盖，密钥全丢。
        # 复制一份做备份（用 copy 而不是 move：坏文件必须留在原地，让下一次
        # _load_raw 继续报这个错，直到用户手动修复——否则错误只闪一次就消失，
        # 用户很可能在不知情的情况下保存，用空配置覆盖）。备份文件已在
        # .gitignore 里挡掉，不会进版本库。
        backup_path = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.corrupted.{int(time.time())}")
        try:
            shutil.copy2(str(CONFIG_PATH), str(backup_path))
            backup_note = f"原文件已备份到 {backup_path}"
        except OSError as backup_err:
            backup_note = f"备份原文件也失败了（{backup_err}），原文件未改动，仍在 {CONFIG_PATH}"
        logger.error("config.json 解析失败: %s；%s", e, backup_note)
        raise ConfigCorruptedError(
            f"配置文件损坏，无法解析为 JSON（{e}）。{backup_note}。请手动检查 {CONFIG_PATH} 修复后再刷新。"
        ) from e
    if not isinstance(data, dict):
        raise ConfigCorruptedError(
            f"配置文件内容不是合法的对象结构（实际是 {type(data).__name__}），请手动检查 {CONFIG_PATH}"
        )
    return data


def _save_raw(data: dict) -> None:
    """原子写入 config.json。

    先在同目录创建一个仅当前用户可读写的临时文件（os.open 时就带 0o600 权限，
    不存在中间的 world-readable 时间窗口），写完整内容后用 os.replace 做原子
    替换——避免进程崩溃/并发写导致文件半写坏，也避免明文密钥在磁盘上短暂裸奔。
    任何一步失败都清理临时文件，不留半成品。
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.with_name(f".{CONFIG_PATH.name}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(str(tmp_path), str(CONFIG_PATH))
    except BaseException:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        raise


def list_channels() -> list[Channel]:
    return [Channel.from_dict(d) for d in _load_raw().get("channels", [])]


def get_channel(channel_id: str) -> Channel | None:
    for c in list_channels():
        if c.id == channel_id:
            return c
    return None


# 更新已有渠道时，这些字段"请求里没提供"就沿用旧值（不是清空/回退成默认值）。
# id/type 不在这里：它们是必填的定位字段，永远会出现在请求里。
_MERGE_ON_UPDATE_FIELDS = (
    "name",
    "base_url",
    "region",
    "organization",
    "project",
    "user_id",
    "enabled",
    "extra",
)
_SECRET_FIELDS = ("api_key", "ak", "sk")


def upsert_channel(data: dict, provided_fields: set[str] | None = None) -> Channel:
    """新建或更新一个渠道。

    provided_fields：请求体里"显式出现过的字段名"集合（调用方从 Pydantic 的
    model_fields_set 拿到），用来区分两种语义：
    - 字段不在 provided_fields 里（哪怕 data 里有值/默认值）→ 沿用旧值。例如
      前端"启用/停用"快捷开关只发 {"id","type","enabled"} 这种最小 payload
      时，name/base_url/region/organization/project 都不该被清空，也不该被
      Channel.from_dict 对缺失字段的默认处理悄悄改写（比如 name 缺失就回退成
      PROVIDERS[type]["default_name"]，enabled 缺失就默认回填 True 意外重新
      启用一个刚被停用的渠道）。
    - 字段在 provided_fields 里但值是空字符串/None → 视为用户显式要清空这个
      可选字段，按新值（空）写入。

    provided_fields 为 None（未指定，兼容旧调用方/测试）时，退化为只保护
    api_key/ak/sk 三个字段的旧行为，其余字段仍按 Channel.from_dict 的默认处理。

    api_key/ak/sk 三个密钥字段是例外，不受 provided_fields 影响：新值为空或者
    是 mask_secret() 打码形态时，一律沿用旧值——前端编辑表单密钥框留空，或者
    把 GET 时拿到的打码串原样回传，都表示"不修改密钥"，不是"清空密钥"。
    """
    raw = _load_raw()
    channel = Channel.from_dict(data)
    channels = raw.setdefault("channels", [])
    for i, c in enumerate(channels):
        if str(c.get("id")) == channel.id:
            old = Channel.from_dict(c)
            for k in _SECRET_FIELDS:
                new_v = getattr(channel, k)
                if not new_v or is_masked_secret(new_v):
                    setattr(channel, k, getattr(old, k))
            if provided_fields is not None:
                for k in _MERGE_ON_UPDATE_FIELDS:
                    if k not in provided_fields:
                        setattr(channel, k, getattr(old, k))
            channels[i] = channel.to_dict(secret=True)
            break
    else:
        channels.append(channel.to_dict(secret=True))
    raw["channels"] = channels
    _save_raw(raw)
    return channel


def delete_channel(channel_id: str) -> bool:
    raw = _load_raw()
    channels = raw.get("channels", [])
    new_channels = [c for c in channels if str(c.get("id")) != channel_id]
    if len(new_channels) == len(channels):
        return False
    raw["channels"] = new_channels
    _save_raw(raw)
    return True


def validate_base_url(url: str | None) -> str | None:
    """校验并规整 base_url（去掉尾部斜杠）。"""
    if not url:
        return None
    url = url.strip().rstrip("/")
    if not re.match(r"^https?://", url):
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
    return url


# ── 配置导入/导出 ─────────────────────────────────────────────


def export_config(include_secrets: bool = False) -> dict:
    """导出当前配置。

    安全默认：include_secrets 默认 **False**（脱敏导出）——一个无认证的本地 GET
    端点默认吐明文密钥是危险的（可被浏览器缓存进历史记录、被 DNS rebinding 跨站
    读取）。需要密钥时必须显式传 include_secrets=True。

    include_secrets=True：密钥原样导出（适合个人备份/换机迁移）。
    include_secrets=False（默认）：密钥字段整体丢弃（导出的是"渠道结构模板"，
      可安全分享给他人参考配置，但对方需要自己填密钥）。
    """
    channels = list_channels()
    if include_secrets:
        items = [c.to_dict(secret=True) for c in channels]
    else:
        # 脱敏导出：密钥字段不导出，让导入方自己填（而不是导出打码串——
        # 打码串导入后会被当成真实密钥，查询时必失败）。
        items = []
        for c in channels:
            d = c.to_dict(secret=False)
            for k in ("api_key", "ak", "sk"):
                d.pop(k, None)
            items.append(d)
    return {
        "version": 1,
        "exported_at": int(time.time() * 1000),
        "channels": items,
    }


class ImportConfigError(ValueError):
    """导入的配置文件结构不合法。"""


def import_config(data: dict, mode: str = "merge") -> dict:
    """导入配置。

    mode:
    - "merge"（默认）：把导入的渠道追加到现有配置；同 id 的渠道用导入的覆盖。
      密钥字段：导入数据里没有的密钥，沿用现有值（和编辑渠道时"留空表示不修改"一致）。
    - "replace"：清空现有全部渠道，用导入的替换。危险操作，前端会二次确认。

    返回导入后的渠道数。
    """
    if not isinstance(data, dict):
        raise ImportConfigError("导入内容不是合法的对象结构")
    incoming = data.get("channels")
    if not isinstance(incoming, list):
        raise ImportConfigError("导入内容缺少 channels 列表或格式不正确")

    raw = _load_raw()
    existing = raw.setdefault("channels", [])

    # 校验每条导入记录的 type 合法（非法 type 的渠道会污染注册表，必须挡掉）
    validated: list[dict] = []
    for i, item in enumerate(incoming):
        if not isinstance(item, dict):
            raise ImportConfigError(f"第 {i + 1} 条渠道不是对象结构")
        ctype = item.get("type")
        if ctype not in PROVIDERS:
            raise ImportConfigError(f"第 {i + 1} 条渠道的 type「{ctype}」不是已知渠道类型")
        validated.append(item)

    if mode == "replace":
        existing = []

    for item in validated:
        # 导入的渠道如果没有 id 或 id 已存在，走 upsert 语义；
        # 全新 id 直接追加。密钥缺失时沿用现有同 id 渠道的密钥。
        cid = str(item.get("id") or f"ch_{uuid.uuid4().hex[:10]}")
        item["id"] = cid
        # merge 模式下，如果导入数据没带密钥，尝试从现有同 id 渠道继承
        if mode == "merge":
            for existing_ch in existing:
                if str(existing_ch.get("id")) == cid:
                    for k in ("api_key", "ak", "sk"):
                        if not item.get(k) and existing_ch.get(k):
                            item[k] = existing_ch[k]
                    break
        # base_url 规整（去尾斜杠），与手动创建渠道保持一致——避免导入的
        # "https://x.com/" 在后续 URL 拼接时产生双斜杠。
        if item.get("base_url"):
            try:
                item["base_url"] = validate_base_url(item["base_url"])
            except ValueError:
                pass  # 导入时不因 base_url 格式问题整体失败，留给查询时报错
        # 去掉已有的同 id 记录（覆盖语义），再追加新的
        existing = [c for c in existing if str(c.get("id")) != cid]
        existing.append(item)

    raw["channels"] = existing
    _save_raw(raw)
    return {"count": len(existing)}
