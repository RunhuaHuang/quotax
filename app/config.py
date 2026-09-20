"""渠道配置存储：config.json 保存在项目根目录，权限 600。

只有 API Key / 火山 AK/SK / 中转站地址这类需要用户手动填写的敏感信息
才会存进这里；订阅类渠道（Claude / Gemini / Grok / Codex / Copilot）
一律直接读取各 CLI 自己的凭据文件，本项目不复制、不写入任何凭据。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# config.json 是一个小型本地配置仓库，但 FastAPI 的同步端点、to_thread 后台任务、
# OAuth 回调线程都可能同时执行“读 → 修改 → 原子替换”。os.replace 只能保证单次
# 写入不会留下半个 JSON，不能防止两个读改写事务互相覆盖（典型丢更新：A、B 都读
# 到旧版本，A 先写入渠道 a，B 随后用自己的旧快照写入渠道 b，渠道 a 被静默抹掉）。
# 用进程内 RLock 把完整读改写事务串行化；RLock 允许 get_channel → list_channels
# 这类嵌套读取，且符合本项目“单进程本机服务”的部署模型。
_config_lock = threading.RLock()

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
# api_key / ak / sk / base_url / workspace_id 才是各渠道自己 fields 列表里出现时的必填项。
OPTIONAL_FIELD_NAMES = {"region", "organization", "project", "user_id", "workspace_id"}
# 个别渠道还可以在自己的 PROVIDERS 条目里声明 optional_fields（如火山：AK/SK
# 留空时查询层自动回退本机 arkcli 的 SSO 登录态），新建时不强制必填。

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
    "zhipu_balance": {
        "category": "balance",
        "label": "智谱 API 余额",
        "fields": ["api_key"],
        "default_name": "智谱 API 余额",
        "manage_url": "https://bigmodel.cn/usercenter/finance",
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
        # AK/SK 可不填：未配置时查询层自动回退本机 arkcli 的 SSO 登录态
        #（arkcli auth login，见 volcengine._query_via_arkcli）
        "optional_fields": ["ak", "sk"],
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
    "bailian": {
        # api_key 借用为 Cookie 存储（同 mimo/opencode）；region: cn（默认，大陆
        # 个人版）/ intl（国际 Model Studio 个人版）
        "category": "coding_plan",
        "label": "阿里云百炼 Token Plan",
        "fields": ["api_key", "region"],
        "default_name": "百炼 Token Plan",
        "manage_url": "https://bailian.console.aliyun.com/?tab=plan#/efm/subscription/token-plan/personal",
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
    "opencode_subscription": {
        "category": "subscription",
        "label": "OpenCode (Zen/Go) 订阅",
        # api_key 借用为 Cookie 存储（同 mimo）；workspace_id 可选——不填时自动从 Cookie 探测
        # 但必须在配置表单中可见，以便自动探测失败时用户可以手动补填。
        "fields": ["api_key", "workspace_id"],
        "default_name": "OpenCode 订阅",
        "manage_url": "https://opencode.ai/",
    },
    "cursor_subscription": {
        # 自动读本机 Cursor 的 state.vscdb 登录态（无需任何配置），同 Claude/
        # Gemini 等订阅渠道的「只读凭据」模式
        "category": "subscription",
        "label": "Cursor 订阅",
        "fields": [],
        "default_name": "Cursor 订阅",
        "manage_url": "https://cursor.com/settings",
    },
}

CATEGORY_LABELS = {
    "balance": "余额（API Key）",
    "coding_plan": "Coding Plan 额度",
    "subscription": "订阅用量（只读凭据）",
    "local": "本地统计",
}


# 后台监控设置和渠道配置一起持久化，但默认关闭。这样升级现有安装不会在用户未
# 授权的情况下自动查询上游或弹系统通知；用户在设置页显式开启后才启动周期检查。
DEFAULT_MONITOR_SETTINGS = {
    "enabled": False,
    "interval_seconds": 300,
    "desktop_notifications": True,
    "webhook_url": "",
    "cooldown_seconds": 3600,
    "retention_days": 365,
    "thresholds": {},
}

# 火山渠道在展示层会拆成 <id>_agent / <id>_coding 两张卡，但配置层只保存
# 不带后缀的原始渠道。CLI、Web API 和监控清理都需要同一套归一规则；放在配置
# 模块里可以避免 CLI 为了调用 Web 端 helper 而导入 FastAPI，也避免多处逻辑漂移。
VOLC_PLAN_SUFFIXES = ("_agent", "_coding")

MONITOR_INTERVAL_MIN_SECONDS = 60
MONITOR_INTERVAL_MAX_SECONDS = 86_400
MONITOR_COOLDOWN_MIN_SECONDS = 60
MONITOR_COOLDOWN_MAX_SECONDS = 604_800
USAGE_RETENTION_MIN_DAYS = 1
USAGE_RETENTION_MAX_DAYS = 3650


class SettingsValidationError(ValueError):
    """监控设置结构或取值不合法。"""


def _bounded_int(value, *, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise SettingsValidationError(f"{field_name} 必须是整数")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise SettingsValidationError(f"{field_name} 必须是整数")
        parsed = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        parsed = int(value.strip())
    else:
        raise SettingsValidationError(f"{field_name} 必须是整数")
    if parsed < minimum or parsed > maximum:
        raise SettingsValidationError(f"{field_name} 必须在 {minimum} 到 {maximum} 之间")
    return parsed


def _normalize_thresholds(value) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SettingsValidationError("thresholds 必须是渠道 ID 到百分比的对象")
    normalized: dict[str, float] = {}
    for raw_channel_id, raw_threshold in value.items():
        channel_id = str(raw_channel_id).strip()
        if not channel_id:
            raise SettingsValidationError("thresholds 中的渠道 ID 不能为空")
        if len(channel_id) > 256 or any(ord(ch) < 32 for ch in channel_id):
            raise SettingsValidationError("thresholds 中的渠道 ID 过长或包含控制字符")
        if isinstance(raw_threshold, bool):
            raise SettingsValidationError(f"渠道 {channel_id} 的阈值必须是数字")
        try:
            threshold = float(raw_threshold)
        except (TypeError, ValueError) as e:
            raise SettingsValidationError(f"渠道 {channel_id} 的阈值必须是数字") from e
        if not math.isfinite(threshold):
            raise SettingsValidationError(f"渠道 {channel_id} 的阈值必须是有限数字")
        if threshold < 0 or threshold > 100:
            raise SettingsValidationError(f"渠道 {channel_id} 的阈值必须在 0 到 100 之间")
        normalized[channel_id] = round(threshold, 1)
    return normalized


def normalize_monitor_settings(value: dict | None) -> dict:
    """把旧配置/部分配置规整成完整且有边界的监控设置。"""
    raw = value or {}
    if not isinstance(raw, dict):
        raise SettingsValidationError("settings.monitor 必须是对象")
    allowed_fields = set(DEFAULT_MONITOR_SETTINGS)
    unknown = set(raw) - allowed_fields
    if unknown:
        raise SettingsValidationError(f"未知监控设置字段: {', '.join(sorted(unknown))}")
    result = copy.deepcopy(DEFAULT_MONITOR_SETTINGS)
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise SettingsValidationError("enabled 必须是布尔值")
        result["enabled"] = raw["enabled"]
    if "desktop_notifications" in raw:
        if not isinstance(raw["desktop_notifications"], bool):
            raise SettingsValidationError("desktop_notifications 必须是布尔值")
        result["desktop_notifications"] = raw["desktop_notifications"]
    if "interval_seconds" in raw:
        result["interval_seconds"] = _bounded_int(
            raw["interval_seconds"],
            field_name="interval_seconds",
            minimum=MONITOR_INTERVAL_MIN_SECONDS,
            maximum=MONITOR_INTERVAL_MAX_SECONDS,
        )
    if "cooldown_seconds" in raw:
        result["cooldown_seconds"] = _bounded_int(
            raw["cooldown_seconds"],
            field_name="cooldown_seconds",
            minimum=MONITOR_COOLDOWN_MIN_SECONDS,
            maximum=MONITOR_COOLDOWN_MAX_SECONDS,
        )
    if "retention_days" in raw:
        result["retention_days"] = _bounded_int(
            raw["retention_days"],
            field_name="retention_days",
            minimum=USAGE_RETENTION_MIN_DAYS,
            maximum=USAGE_RETENTION_MAX_DAYS,
        )
    if "webhook_url" in raw:
        webhook_url = _validate_http_url(
            raw["webhook_url"], field_name="webhook_url", allow_empty=True, max_length=2048
        )
        result["webhook_url"] = webhook_url
    if "thresholds" in raw:
        result["thresholds"] = _normalize_thresholds(raw["thresholds"])
    return result


def _merge_monitor_settings(current: dict | None, patch: dict | None) -> dict:
    merged = normalize_monitor_settings(current)
    if patch is None:
        return merged
    if not isinstance(patch, dict):
        raise SettingsValidationError("settings.monitor 必须是对象")
    merged.update(patch)
    return normalize_monitor_settings(merged)


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
    workspace_id: str | None = None  # OpenCode 工作区 ID（wrk_xxx），拼进官网 SSR 页面路径
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
        if self.workspace_id:
            d["workspace_id"] = self.workspace_id
        if self.extra:
            d["extra"] = copy.deepcopy(self.extra) if secret else _redact_sensitive(self.extra)
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
        if not isinstance(d, dict):
            raise ConfigCorruptedError("渠道记录必须是对象结构")
        ctype = d.get("type")
        if ctype not in PROVIDERS:
            raise ConfigCorruptedError(f"渠道 type「{ctype}」不是已知渠道类型")
        enabled = d.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigCorruptedError("渠道 enabled 必须是布尔值")
        extra = d.get("extra")
        if extra is None:
            extra = {}
        if not isinstance(extra, dict):
            raise ConfigCorruptedError("渠道 extra 必须是对象")

        def optional_text(name: str):
            value = d.get(name)
            if value is None:
                return None
            if not isinstance(value, str):
                raise ConfigCorruptedError(f"渠道 {name} 必须是字符串")
            return value

        raw_id = d.get("id")
        channel_id = f"ch_{uuid.uuid4().hex[:10]}" if raw_id in (None, "") else str(raw_id)
        if len(channel_id) > 256 or any(ord(ch) < 32 for ch in channel_id):
            raise ConfigCorruptedError("渠道 id 过长或包含控制字符")
        name = d.get("name")
        if name is None or name == "":
            name = PROVIDERS[ctype].get("default_name", ctype)
        elif not isinstance(name, str):
            raise ConfigCorruptedError("渠道 name 必须是字符串")
        base_url = optional_text("base_url")
        if base_url:
            try:
                base_url = validate_base_url(base_url)
            except ValueError as e:
                raise ConfigCorruptedError(f"渠道 Base URL 不合法: {e}") from e
        return cls(
            id=channel_id,
            type=ctype,
            name=name,
            api_key=optional_text("api_key"),
            base_url=base_url,
            ak=optional_text("ak"),
            sk=optional_text("sk"),
            region=optional_text("region"),
            organization=optional_text("organization"),
            project=optional_text("project"),
            user_id=optional_text("user_id"),
            workspace_id=optional_text("workspace_id"),
            enabled=enabled,
            extra=copy.deepcopy(extra),
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


_SENSITIVE_KEY_NAMES = {
    "api_key",
    "ak",
    "sk",
    "cookie",
    "auth",
    "authorization",
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "credentials",
    "webhook_url",
}


def _is_sensitive_key(key: str) -> bool:
    # 额外字段来自不同 provider，常见命名同时包含 snake_case、kebab-case 和
    # camelCase（如 accessToken / refreshToken）。统一拆分驼峰并把其它分隔符规整
    # 成下划线，避免安全导出只识别一种命名风格而漏出嵌套 token。
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key).strip())
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").lower()
    if normalized == "codex_auth_file":
        # 这是 credentials/ 下的相对文件名，不是凭据内容；保留它才能让前端诊断
        # 和旧版迁移继续工作。文件读取/删除仍由 resolve_codex_auth_file 限制范围。
        return False
    return normalized in _SENSITIVE_KEY_NAMES or any(
        part in normalized.split("_")
        for part in ("token", "secret", "password", "credential", "cookie", "auth")
    )


def _redact_sensitive(value, *, key: str | None = None):
    """递归脱敏配置扩展字段，避免 Cookie/Token 藏在嵌套 extra 中泄露。"""
    if key is not None and _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact_sensitive(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive(item) for item in value]
    return copy.deepcopy(value)


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
    channels = data.get("channels", [])
    if not isinstance(channels, list):
        raise ConfigCorruptedError("配置文件 channels 必须是数组，请手动检查 config.json")
    if any(not isinstance(item, dict) for item in channels):
        raise ConfigCorruptedError("配置文件 channels 中每条记录必须是对象，请手动检查 config.json")
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
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(CONFIG_PATH))
    except BaseException:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        raise


def list_channels() -> list[Channel]:
    with _config_lock:
        return [Channel.from_dict(d) for d in _load_raw().get("channels", [])]


def get_channel(channel_id: str) -> Channel | None:
    with _config_lock:
        for c in list_channels():
            if c.id == channel_id:
                return c
    return None


def canonical_channel_id(channel_id: str) -> str:
    """把展示层可能带 ``_agent``/``_coding`` 后缀的 ID 归一到配置 ID。

    精确匹配永远优先，避免误伤真实存在且本身以这些后缀结尾的普通渠道；只有
    精确匹配失败、且去掉后缀后的渠道确实是火山类型时才做归一。
    """
    raw = str(channel_id)
    if get_channel(raw) is not None:
        return raw
    for suffix in VOLC_PLAN_SUFFIXES:
        if raw.endswith(suffix):
            base_id = raw[: -len(suffix)]
            base_channel = get_channel(base_id)
            if base_channel is not None and base_channel.type == "volcengine":
                return base_id
    return raw


def get_settings() -> dict:
    """读取完整应用设置；旧配置没有 settings 时自动补默认值但不立刻写盘。"""
    with _config_lock:
        raw_settings = _load_raw().get("settings") or {}
        if not isinstance(raw_settings, dict):
            raise SettingsValidationError("settings 必须是对象")
        return {"monitor": normalize_monitor_settings(raw_settings.get("monitor"))}


def update_settings(data: dict) -> dict:
    """部分更新应用设置并原子持久化，保留未提供的字段。"""
    if not isinstance(data, dict):
        raise SettingsValidationError("设置内容必须是对象")
    unknown = set(data) - {"monitor"}
    if unknown:
        raise SettingsValidationError(f"未知设置字段: {', '.join(sorted(unknown))}")
    with _config_lock:
        raw = _load_raw()
        current_settings = raw.get("settings") or {}
        if not isinstance(current_settings, dict):
            raise SettingsValidationError("settings 必须是对象")
        monitor = _merge_monitor_settings(current_settings.get("monitor"), data.get("monitor"))
        raw["settings"] = {"monitor": monitor}
        _save_raw(raw)
        return copy.deepcopy(raw["settings"])


# 更新已有渠道时，这些字段"请求里没提供"就沿用旧值（不是清空/回退成默认值）。
# id/type 不在这里：它们是必填的定位字段，永远会出现在请求里。
_MERGE_ON_UPDATE_FIELDS = (
    "name",
    "base_url",
    "region",
    "organization",
    "project",
    "user_id",
    "workspace_id",
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

    extra 字段同样有脱敏保护：to_dict(secret=False) 会把 extra 里的嵌套
    Cookie/Token（cookie/access_token/refreshToken 等）替换成 [REDACTED]。若客户端
    把 GET 拿到的对象原样 PUT 回来（extra 被显式提供，或 provided_fields 为 None），
    会按字段/列表位置从旧值恢复真实凭据（_restore_redacted_values），与 import_config
    保持一致，避免占位符静默覆盖真实 Cookie/Token。
    """
    with _config_lock:
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
                # extra 里的嵌套 Cookie/Token 在 GET 脱敏后（to_dict(secret=False)）会
                # 变成 [REDACTED]。若客户端把 GET 拿到的对象原样 PUT 回来（extra 被显式
                # 提供，或 provided_fields 为 None 的旧调用路径），会把占位符当真实凭据
                # 写入、静默覆盖真实 Cookie/Token。按字段/列表位置从旧值恢复，与
                # import_config 保持一致；provided_fields 未提供 extra 时上面已沿用旧值。
                if provided_fields is None or "extra" in (provided_fields or ()):
                    restored = _restore_redacted_values(old.extra, channel.extra)
                    channel.extra = {} if restored is _MISSING else restored
                channels[i] = channel.to_dict(secret=True)
                break
        else:
            channels.append(channel.to_dict(secret=True))
        raw["channels"] = channels
        _save_raw(raw)
        _cleanup_unreferenced_codex_credentials(channels)
        return channel


def delete_channel(channel_id: str) -> bool:
    with _config_lock:
        raw = _load_raw()
        channels = raw.get("channels", [])
        new_channels = [c for c in channels if str(c.get("id")) != channel_id]
        if len(new_channels) == len(channels):
            return False
        raw["channels"] = new_channels
        _save_raw(raw)
        _cleanup_unreferenced_codex_credentials(new_channels)
        return True


def validate_base_url(url: str | None) -> str | None:
    """校验并规整 base_url（去掉尾部斜杠）。"""
    if not url:
        return None
    try:
        return _validate_http_url(url, field_name="Base URL", allow_empty=False).rstrip("/")
    except SettingsValidationError as e:
        raise ValueError(str(e)) from e


def _validate_http_url(
    value, *, field_name: str, allow_empty: bool, max_length: int = 4096
) -> str:
    if value is None and allow_empty:
        return ""
    if not isinstance(value, str):
        raise SettingsValidationError(f"{field_name} 必须是 URL 字符串")
    url = value.strip()
    if not url and allow_empty:
        return ""
    if not url or len(url) > max_length:
        raise SettingsValidationError(f"{field_name} 为空或过长")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise SettingsValidationError(f"{field_name} 不能包含空白或控制字符")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as e:
        raise SettingsValidationError(f"{field_name} 格式不正确") from e
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise SettingsValidationError(f"{field_name} 必须是完整的 http:// 或 https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise SettingsValidationError(f"{field_name} 不允许在 URL 中携带用户名或密码")
    if parsed.fragment:
        raise SettingsValidationError(f"{field_name} 不允许包含 fragment")
    if port is not None and not (1 <= port <= 65535):
        raise SettingsValidationError(f"{field_name} 端口不正确")
    # 保存阶段只拒绝明确的本机/私网字面地址；域名解析受当前网络、代理和 DNS
    # 环境影响，运行时网络层会再次解析并拒绝当前解析到的私网地址。解析检查与
    # 实际连接之间仍存在底层网络栈无法完全消除的极短 TOCTOU 窗口，因此这里的
    # 语义是降低 SSRF/DNS rebinding 风险，而不是宣称绝对的 IP 固定。
    assert_public_http_url(url, field_name=field_name, resolve_dns=False)
    return url


_PRIVATE_HOST_NAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
}


def _is_private_or_reserved_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    # 198.18.0.0/15（RFC 2544 基准测试段）是 Clash / sing-box 等代理 TUN
    # fake-ip 模式的事实标准回传段：开启后所有被代理域名在本机都解析成
    # 198.18.x.x，真实解析由代理远端完成。把它当作「代理环境的假 IP」放行——
    # 普通网络下该段不可路由（放行不构成 SSRF 面）；不放行则 fake-ip 用户的
    # 所有境外渠道全挂（实测：api.anthropic.com → 198.18.1.203）。
    if address.version == 4 and address in ipaddress.ip_network("198.18.0.0/15"):
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _parse_public_url(url: str, *, field_name: str) -> tuple[str, int]:
    """解析 URL 并做字面主机/IP 检查，返回 (host, port)；非法则抛 SettingsValidationError。

    只校验字面值（域名/字面 IP），不做 DNS 解析——解析交给调用方决定用什么方式
    （同步 socket.getaddrinfo / 异步 loop.getaddrinfo），避免把阻塞式解析硬塞给
    不需要它的调用路径。
    """
    try:
        parsed = urlsplit(url)
    except ValueError as e:
        raise SettingsValidationError(f"{field_name} 主机名格式不正确") from e
    # 与旧版 assert_public_http_url 保持一致的 URL 合法性校验（重构进本函数时
    # 一度丢失，靠 DNS 校验碰巧兜住——fake-ip 环境下会漏放 ftp:// 等非法 scheme）
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise SettingsValidationError(f"{field_name} 必须是完整的 http:// 或 https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise SettingsValidationError(f"{field_name} 不允许在 URL 中携带用户名或密码")
    if parsed.fragment:
        raise SettingsValidationError(f"{field_name} 不允许包含 fragment")
    try:
        port_value = parsed.port
    except ValueError as e:
        raise SettingsValidationError(f"{field_name} 端口号不合法") from e
    if port_value is not None and not (1 <= port_value <= 65535):
        raise SettingsValidationError(f"{field_name} 端口号必须在 1-65535 之间")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise SettingsValidationError(f"{field_name} 缺少主机名")
    if host in _PRIVATE_HOST_NAMES or _is_private_or_reserved_ip(host):
        raise SettingsValidationError(f"{field_name} 不允许指向本机、内网或链路本地地址")
    port = port_value or (443 if parsed.scheme == "https" else 80)
    return host, port


def assert_public_http_url(
    url: str, *, field_name: str = "URL", resolve_dns: bool = True
) -> None:
    """阻止回环、私网、链路本地和云元数据地址，降低 SSRF 风险。

    配置保存时会检查字面 IP，并尽力解析域名；运行时网络层还会再次解析并拒绝
    当前解析到内网地址的请求，降低 DNS rebinding 在保存后切换地址造成的 SSRF
    风险。由于解析与实际连接由底层网络栈分两步完成，不能把这项检查描述成绝对
    的 IP 固定。DNS 暂时不可解析时不在保存阶段拒绝配置，让用户仍能先保存离线
    配置，真正请求时再给出网络错误。

    注意：本函数是同步的，内部 socket.getaddrinfo 会阻塞调用线程。异步上下文
    里请用 assert_public_http_url_async（事件循环版，不会阻塞事件循环）。
    """
    host, port = _parse_public_url(url, field_name=field_name)
    if not resolve_dns:
        return
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:  # socket.gaierror 是 OSError 的子类，无需单独列出
        return
    for info in infos:
        address = info[4][0]
        if _is_private_or_reserved_ip(address):
            raise SettingsValidationError(f"{field_name} 解析到了本机、内网或链路本地地址")


async def assert_public_http_url_async(url: str, *, field_name: str = "URL") -> None:
    """async 版 URL 校验：字面 IP 检查同步做，DNS 解析走事件循环的 getaddrinfo。

    loop.getaddrinfo 内部把阻塞的 getaddrinfo 丢到线程池执行，因此不会像
    assert_public_http_url 那样在事件循环线程上同步解析域名。网络层每次发上游
    请求前都会做这个校验，如果解析慢（DNS 抽风 / 无网络），同步版会把整个事件
    循环卡住数秒（期间所有 API 无响应）；这里保证校验本身不阻塞其他协程。
    """
    host, port = _parse_public_url(url, field_name=field_name)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except OSError:  # socket.gaierror 是 OSError 的子类，无需单独列出
        return
    for info in infos:
        address = info[4][0]
        if _is_private_or_reserved_ip(address):
            raise SettingsValidationError(f"{field_name} 解析到了本机、内网或链路本地地址")


def resolve_codex_auth_file(rel_path: str) -> Path | None:
    """把渠道 extra.codex_auth_file 解析成绝对路径，并做路径穿越防护。

    codex_auth_file 正常由 upload_codex_credentials 端点生成，值固定为
    `credentials/codex_<channel_id>.json`。但 extra 是用户可通过 POST /api/channels
    自由设置的开放字段，如果不校验，用户（或导入的恶意配置）可以传入
    `../../etc/passwd` 或绝对路径 `/etc/passwd`：读取侧会让 query_codex 去读本目录
    之外的任意文件（违反"只读 CLI 凭据"的边界），更糟的是删除渠道时 main.py 会
    `unlink()` 这个路径——配合一个绝对路径的 codex_auth_file，删除渠道会变成任意
    文件删除。

    这里把传入值拼到 config 同目录后 resolve，并校验结果必须落在 credentials/
    子目录内、文件名匹配 codex_*.json。任一不满足就返回 None（调用方据此走"无
    关联凭据 / 路径非法"的正常分支），绝不返回一个越界的路径。
    """
    if not rel_path or not isinstance(rel_path, str):
        return None
    try:
        if Path(rel_path).is_absolute():
            return None
    except (OSError, ValueError):
        return None
    base = CONFIG_PATH.parent.resolve()
    try:
        resolved = (base / rel_path).resolve()
    except (OSError, ValueError):
        return None
    cred_root = (base / "credentials").resolve()
    # credentials/ 本身若被替换成指向外部目录的符号链接，单纯检查
    # resolved.relative_to(cred_root) 会把外部目录误当成安全根目录；先确认
    # credentials 根仍位于当前配置目录内，避免读取/删除配置目录之外的文件。
    try:
        cred_root.relative_to(base)
    except ValueError:
        return None
    try:
        resolved.relative_to(cred_root)
    except ValueError:
        return None
    if not resolved.name.startswith("codex_") or resolved.suffix != ".json":
        return None
    return resolved


def codex_credentials_path(channel_id: str) -> tuple[Path, str]:
    """返回与渠道 ID 无关的安全凭据文件路径。

    渠道 ID 可来自导入文件，不能直接拼入文件名；使用 SHA-256 派生固定文件名既
    保持同一渠道重复上传时覆盖同一份凭据，也完全消除路径分隔符和 ``..`` 的影响。
    """
    base = CONFIG_PATH.parent.resolve()
    cred_dir = base / "credentials"
    try:
        cred_dir.resolve().relative_to(base)
    except ValueError as e:
        raise ValueError("credentials 目录不能指向配置目录之外") from e

    channel_text = str(channel_id)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", channel_text):
        stem = channel_text
    else:
        stem = hashlib.sha256(channel_text.encode("utf-8", "surrogatepass")).hexdigest()
    rel_path = f"credentials/codex_{stem}.json"
    return base / rel_path, rel_path


def _cleanup_unreferenced_codex_credentials(channels: list[dict]) -> None:
    """清理配置变更后不再被引用的 Codex 凭据文件。

    配置导入/替换或渠道重新关联凭据时，旧版本只更新 ``config.json``，不会删除
    ``credentials/codex_*.json``，久而久之会留下包含 refresh token 的孤儿文件。
    这里只扫描当前配置目录下的 ``credentials/``，并且只处理明确匹配
    ``codex_*.json`` 的文件；越界路径、其它文件和目录一律不碰。

    除了配置中显式引用的路径，还保留所有 Codex 渠道按 ID 派生出的规范路径。
    这样上传接口在“先写文件、后提交关联”这段很短的窗口内，另一个并发配置写入
    不会误删刚写好的新凭据。清理失败只记录日志，不让已经成功的配置变更回滚。
    """
    base = CONFIG_PATH.parent.resolve()
    cred_root = (base / "credentials").resolve()
    if not cred_root.is_dir():
        return
    try:
        cred_root.relative_to(base)
    except ValueError:
        return

    keep: set[Path] = set()
    for item in channels:
        if not isinstance(item, dict):
            continue
        channel_id = item.get("id")
        if channel_id and item.get("type") == "codex_subscription":
            try:
                canonical, _ = codex_credentials_path(str(channel_id))
                keep.add(canonical.resolve())
            except (OSError, ValueError):
                pass
        extra = item.get("extra")
        if isinstance(extra, dict):
            path = resolve_codex_auth_file(extra.get("codex_auth_file"))
            if path is not None:
                keep.add(path)

    for candidate in cred_root.rglob("codex_*.json"):
        if candidate.is_dir():
            continue
        try:
            resolved = candidate.resolve()
            resolved.relative_to(cred_root)
        except (OSError, ValueError):
            continue
        if resolved in keep:
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            logger.warning("清理孤儿 Codex 凭据失败: %s", candidate)


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
    with _config_lock:
        channels = list_channels()
        if include_secrets:
            items = [c.to_dict(secret=True) for c in channels]
        else:
            items = []
            for c in channels:
                d = c.to_dict(secret=False)
                for k in ("api_key", "ak", "sk"):
                    d.pop(k, None)
                items.append(d)
        settings = get_settings()
        if not include_secrets:
            settings["monitor"]["webhook_url"] = ""
        return {
            "version": 2,
            "exported_at": int(time.time() * 1000),
            "channels": items,
            "settings": settings,
        }


class ImportConfigError(ValueError):
    """导入的配置文件结构不合法。"""


_REDACTED_MARKER = "[REDACTED]"
_MISSING = object()


def _restore_redacted_values(existing, incoming):
    """处理安全导出中的 ``[REDACTED]`` 占位符。

    安全导出会保留嵌套字段的结构，但把敏感值替换成占位符。导入时不能把这个
    占位符当成真实 Cookie/Token 写回配置：merge 应按同名字段（列表按原位置）
    恢复当前配置中的值；replace 则直接忽略没有可恢复来源的占位字段。返回
    ``_MISSING`` 表示调用方应删除该字段/列表元素。

    这个函数只处理导入 payload 的 JSON 形状，不修改调用方传入的对象。
    """
    if incoming == _REDACTED_MARKER:
        if existing is _MISSING:
            return _MISSING
        return copy.deepcopy(existing)
    if isinstance(incoming, dict):
        existing_dict = existing if isinstance(existing, dict) else {}
        result = {}
        for key, value in incoming.items():
            old_value = existing_dict.get(key, _MISSING)
            restored = _restore_redacted_values(old_value, value)
            if restored is not _MISSING:
                result[str(key)] = restored
        return result
    if isinstance(incoming, list):
        existing_list = existing if isinstance(existing, list) else []
        result = []
        for index, value in enumerate(incoming):
            old_value = existing_list[index] if index < len(existing_list) else _MISSING
            restored = _restore_redacted_values(old_value, value)
            if restored is not _MISSING:
                result.append(restored)
        return result
    return copy.deepcopy(incoming)


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
    if mode not in {"merge", "replace"}:
        raise ImportConfigError("mode 必须是 merge 或 replace")
    incoming = data.get("channels")
    if not isinstance(incoming, list):
        raise ImportConfigError("导入内容缺少 channels 列表或格式不正确")
    incoming_settings = data.get("settings")
    if incoming_settings is not None and not isinstance(incoming_settings, dict):
        raise ImportConfigError("settings 必须是对象")
    if isinstance(incoming_settings, dict):
        unknown_settings = set(incoming_settings) - {"monitor"}
        if unknown_settings:
            raise ImportConfigError(f"未知设置字段: {', '.join(sorted(unknown_settings))}")

    with _config_lock:
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
            if "enabled" in item and not isinstance(item["enabled"], bool):
                raise ImportConfigError(f"第 {i + 1} 条渠道的 enabled 必须是布尔值")
            if "extra" in item and not isinstance(item["extra"], dict):
                raise ImportConfigError(f"第 {i + 1} 条渠道的 extra 必须是对象")
            # 不直接修改调用方传入的 payload；同一个 dict 可能还会被日志、重试或
            # 测试复用，导入函数不应偷偷给它补 id/密钥或规整 URL。
            validated.append(dict(item))

        if mode == "replace":
            existing = []

        for item in validated:
            # 导入的渠道如果没有 id 或 id 已存在，走 upsert 语义；
            # 全新 id 直接追加。密钥缺失时沿用现有同 id 渠道的密钥。
            cid = str(item.get("id") or f"ch_{uuid.uuid4().hex[:10]}")
            item["id"] = cid
            existing_item = next(
                (c for c in existing if str(c.get("id")) == cid),
                None,
            )
            # merge 模式下，如果导入数据没带密钥，尝试从现有同 id 渠道继承
            if mode == "merge":
                if existing_item is not None:
                    for k in ("api_key", "ak", "sk"):
                        if (
                            (not item.get(k)
                             or is_masked_secret(item.get(k))
                             or item.get(k) == _REDACTED_MARKER)
                            and existing_item.get(k)
                        ):
                            item[k] = existing_item[k]
            else:
                for k in ("api_key", "ak", "sk"):
                    if is_masked_secret(item.get(k)) or item.get(k) == _REDACTED_MARKER:
                        item.pop(k, None)
            if isinstance(item.get("extra"), dict):
                # 安全导出的嵌套 Cookie/Token 会是 [REDACTED]。merge 从同 ID
                # 旧渠道按字段/列表位置恢复；replace 没有旧值可恢复时删除占位字段，
                # 避免把占位文本误当成可用凭据写入配置。
                old_extra = (
                    existing_item.get("extra")
                    if isinstance(existing_item, dict)
                    else _MISSING
                )
                item["extra"] = _restore_redacted_values(old_extra, item["extra"])
                if item["extra"] is _MISSING:
                    item.pop("extra", None)
            # base_url 规整（去尾斜杠），与手动创建渠道保持一致——避免导入的
            # "https://x.com/" 在后续 URL 拼接时产生双斜杠。
            if item.get("base_url"):
                try:
                    item["base_url"] = validate_base_url(item["base_url"])
                except ValueError as e:
                    raise ImportConfigError(f"渠道 {cid} 的 Base URL 不合法: {e}") from e
            # 经 Channel 往返一次，统一默认名称/布尔值/extra，并丢弃未知顶层字段。
            item = Channel.from_dict(item).to_dict(secret=True)
            # 去掉已有的同 id 记录（覆盖语义），再追加新的
            existing = [c for c in existing if str(c.get("id")) != cid]
            existing.append(item)

        raw["channels"] = existing
        try:
            if mode == "replace":
                # replace 表示替换整个可导出的应用配置；老版本备份没有 settings 时
                # 使用安全默认（监控关闭），不会沿用当前机器上的通知地址。
                monitor = normalize_monitor_settings(
                    (incoming_settings or {}).get("monitor") if incoming_settings else None
                )
                raw["settings"] = {"monitor": monitor}
            elif incoming_settings is not None:
                current_settings = raw.get("settings") or {}
                if not isinstance(current_settings, dict):
                    raise SettingsValidationError("settings 必须是对象")
                incoming_monitor = incoming_settings.get("monitor")
                if isinstance(incoming_monitor, dict):
                    incoming_monitor = dict(incoming_monitor)
                    # 脱敏导出会把 Webhook URL 清空；merge 导入这种文件时，空值
                    # 代表“备份中未包含秘密”，不能把当前机器已有的真实 URL 清掉。
                    if not incoming_monitor.get("webhook_url"):
                        incoming_monitor.pop("webhook_url", None)
                monitor = _merge_monitor_settings(
                    current_settings.get("monitor"), incoming_monitor
                )
                raw["settings"] = {"monitor": monitor}
        except SettingsValidationError as e:
            raise ImportConfigError(str(e)) from e
        _save_raw(raw)
        _cleanup_unreferenced_codex_credentials(existing)
        return {"count": len(existing), "settings_imported": incoming_settings is not None}
