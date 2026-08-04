"""QuotaX — FastAPI 主应用。

启动：uv run uvicorn app.main:app --port 8900
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import config as config_store
from . import credentials as credentials_store
from . import history as history_store
from . import local_usage
from .credentials import CRED_OK
from .models import ChannelResult, fail
from .net import aclose, friendly_error
from .providers import channel_category, query_channel

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# 结果缓存：按渠道 id 分别缓存，成功 60s / 失败 15s（对齐 cc-switch：错误短缓存
# 以便快速重试，同时避免高频打官方接口触发风控）。之前是整体 all-or-nothing——
# 任一渠道失败就把全局 TTL 都降到 15s，导致成功的渠道也被牵连着每 15 秒重查。
QUOTA_CACHE_TTL_MS = 60_000
QUOTA_ERROR_CACHE_TTL_MS = 15_000
_cache: dict[str, tuple[float, dict]] = {}
_inflight: dict[str, asyncio.Task] = {}
_cache_lock = asyncio.Lock()

# 后台趋势记录任务的强引用集合。asyncio.create_task 返回的 Task 若不持有强引用，
# 可能在完成前被事件循环 GC 掉（CPython 官方文档明确警告）；app shutdown 时这些
# 未完成的任务也会被取消。这里集中持有，add_done_callback 完成后自动移除。
_bg_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    """提交一个后台任务并持有强引用，防止被 GC；完成后自动从集合移除。"""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await aclose()


app = FastAPI(title="QuotaX", lifespan=lifespan)


def _format_validation_error(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", []) if x != "body")
        msg = "缺少必填字段" if err.get("type") == "missing" else err.get("msg", "")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "；".join(parts) or "请求参数校验失败"


@app.exception_handler(RequestValidationError)
async def _on_validation_error(_request: Request, exc: RequestValidationError):
    # FastAPI 对 Pydantic 模型校验失败默认返回 422；这个项目统一用 400 表示
    # "请求本身就不对"，并且要带中文 detail，所以在这里把 422 改写成 400。
    return JSONResponse(status_code=400, content={"detail": _format_validation_error(exc)})


@app.exception_handler(config_store.ConfigCorruptedError)
async def _on_config_corrupted(_request: Request, exc: config_store.ConfigCorruptedError):
    # config.json 解析失败时绝不能假装"没有渠道"（那样一保存就会用空配置覆盖，
    # 密钥全丢）。这里统一把错误暴露给前端展示，而不是一个裸的 500 堆栈。
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# ── 请求体模型 ───────────────────────────────────────────────


class ChannelPayload(BaseModel):
    """POST /api/channels 请求体的结构校验（类型是否存在、字段类型是否正确）。

    "新建渠道时哪些字段必填"这个判断依赖当前已存储的渠道列表（编辑已有渠道时
    密钥字段允许留空，表示沿用旧值），属于业务逻辑而不是纯结构校验，放在端点
    函数里处理，不适合塞进这个模型。
    """

    id: str | None = None
    type: str
    name: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    ak: str | None = None
    sk: str | None = None
    region: str | None = None
    organization: str | None = None
    project: str | None = None
    user_id: str | None = None
    enabled: bool = True
    # dict | None（不是单纯 dict）：允许一个把所有字段都发过来、空值发 null 的
    # 前端显式传 "extra": null，而不是被结构校验拒掉；config_store.Channel.
    # from_dict 里 `d.get("extra") or {}` 本来就会把 None 当空 dict 处理。
    extra: dict | None = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _type_must_be_known(cls, v: str) -> str:
        if v not in config_store.PROVIDERS:
            raise ValueError(f"未知渠道类型: {v}")
        return v


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/providers")
async def providers():
    """渠道类型目录（前端配置表单用）。"""
    return {
        "providers": config_store.PROVIDERS,
        "categories": config_store.CATEGORY_LABELS,
    }


@app.get("/api/channels")
async def list_channels():
    """渠道列表（密钥打码）。"""
    return [c.to_dict(secret=False) for c in config_store.list_channels()]


@app.post("/api/channels")
async def create_or_update_channel(payload: ChannelPayload):
    """新建或更新一个渠道。

    校验规则：type 必须在 config.PROVIDERS 里（上面 Pydantic 模型已经挡掉）；
    新建时按 PROVIDERS[type]["fields"] 校验必填项——api_key/ak/sk/base_url 是
    必填，region/organization/project/user_id 永远可选；但编辑已有渠道时密钥
    字段允许为空（表示沿用旧值），所以这个"必填"只在新建时强制。
    """
    # 火山渠道在前端按 Plan 拆成 <id>_agent / <id>_coding 展示，前端开关发的 id 带
    # 这些后缀。停用/启用只需归一回真实 config id（不带后缀），否则会被当成新建。
    if payload.id:
        for suffix in ("_agent", "_coding"):
            if payload.id.endswith(suffix):
                payload.id = payload.id[: -len(suffix)]
                break

    is_new = not payload.id or config_store.get_channel(payload.id) is None
    if is_new:
        required = [
            f
            for f in config_store.PROVIDERS[payload.type].get("fields", [])
            if f not in config_store.OPTIONAL_FIELD_NAMES
        ]
        missing = [f for f in required if not getattr(payload, f, None)]
        if missing:
            raise HTTPException(status_code=400, detail=f"新建渠道缺少必填字段: {', '.join(missing)}")

    data = payload.model_dump()
    try:
        if data.get("base_url"):
            data["base_url"] = config_store.validate_base_url(data["base_url"])
        # model_fields_set：请求体里显式出现过的字段名（区分"没提供"和"提供了
        # 空值"）。比如前端启用/停用开关只发 {"id","type","enabled"}，name/
        # base_url 等字段不该被这次更新清空——见 config_store.upsert_channel。
        channel = config_store.upsert_channel(data, provided_fields=payload.model_fields_set)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await _invalidate_cache(channel.id)
    return channel.to_dict(secret=False)


@app.delete("/api/channels/{channel_id}")
async def remove_channel(channel_id: str):
    # 删除渠道前先拿到它关联的上传凭据文件，渠道删掉后就查不到了
    channel = config_store.get_channel(channel_id)
    if not config_store.delete_channel(channel_id):
        raise HTTPException(status_code=404, detail="渠道不存在")
    await _invalidate_cache(channel_id)
    history_store.delete_channel_history(channel_id)
    if channel and (channel.extra or {}).get("codex_auth_file"):
        # 清理上传的 Codex 凭据文件（私有文件，渠道删除后没有保留的必要）
        try:
            (Path(config_store.CONFIG_PATH.parent) / channel.extra["codex_auth_file"]).unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}


@app.get("/api/channels/{channel_id}/secret")
async def get_channel_secret(channel_id: str):
    """返回指定渠道的明文密钥（仅本机无认证访问，供编辑表单"显示密钥"使用）。"""
    # 火山子渠道 id 带 _agent/_coding 后缀，归一到真实 config id
    base_id = channel_id
    for suffix in ("_agent", "_coding"):
        if base_id.endswith(suffix):
            base_id = base_id[: -len(suffix)]
            break
    channel = config_store.get_channel(base_id)
    if not channel:
        raise HTTPException(status_code=404, detail="渠道不存在")
    secret = {}
    for k in ("api_key", "ak", "sk"):
        v = getattr(channel, k, None)
        if v:
            secret[k] = v
    return {"id": base_id, "secret": secret}


# ── Codex auth.json 上传（多账号） ──────────────────────────────


class CodexCredentialPayload(BaseModel):
    """用户上传的 Codex auth.json 内容（与 ~/.codex/auth.json 同格式）。"""

    content: str


@app.post("/api/channels/{channel_id}/codex-credentials")
async def upload_codex_credentials(channel_id: str, payload: CodexCredentialPayload):
    """上传一份 Codex auth.json 并关联到渠道（同一个账号可以有多个 Codex 渠道，
    每个渠道一份独立凭据，互不覆盖）。

    凭据内容存到 config 同目录 credentials/codex_<channel_id>.json（原子写入、
    权限 600，与 config.json 同策略），渠道 extra.codex_auth_file 记录相对路径；
    query_codex 优先读这份文件，否则读本机 Codex CLI 登录态。
    """
    channel = config_store.get_channel(channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="渠道不存在")
    if channel.type != "codex_subscription":
        raise HTTPException(status_code=400, detail="只有 Codex 订阅渠道支持上传 auth.json")
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="凭据内容为空")
    if len(content) > 512 * 1024:
        raise HTTPException(status_code=400, detail="凭据文件过大（超过 512KB）")

    # 上传前先校验：必须是合法 JSON 且含 ChatGPT OAuth token（复用凭据解析逻辑，
    # 否则存了也查不了额度）
    cred = credentials_store.parse_codex_credentials(content, "上传校验")
    if cred.status != CRED_OK:
        raise HTTPException(status_code=400, detail=f"凭据文件无效: {cred.message or cred.status}")

    cred_dir = config_store.CONFIG_PATH.parent / "credentials"
    cred_dir.mkdir(parents=True, exist_ok=True)
    rel_path = f"credentials/codex_{channel_id}.json"
    path = cred_dir / f"codex_{channel_id}.json"
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
    except BaseException:
        try:
            os.unlink(str(path))
        except OSError:
            pass
        raise

    extra = dict(channel.extra)
    extra["codex_auth_file"] = rel_path
    try:
        config_store.upsert_channel(
            {"id": channel.id, "type": channel.type, "extra": extra},
            provided_fields={"id", "type", "extra"},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await _invalidate_cache(channel_id)
    return {"ok": True, "source": str(path), "account_id": cred.extra.get("account_id")}


# ── /api/quotas：按渠道 id 分别缓存 + 请求合并 ─────────────────


async def _invalidate_cache(channel_id: str) -> None:
    async with _cache_lock:
        _cache.pop(channel_id, None)


def _disabled_result(channel: config_store.Channel) -> dict:
    """停用渠道：不发起任何网络请求，直接标记 status="disabled"。"""
    return ChannelResult(
        id=channel.id,
        type=channel.type,
        name=channel.name,
        category=channel_category(channel.type),
        status="disabled",
    ).to_dict()


async def _query_and_cache(channel: config_store.Channel) -> dict:
    try:
        result = await query_channel(channel)
        payload = result.to_dict()
    except Exception as e:  # query_channel 内部已经兜底大部分异常，这里是双重保险
        payload = fail(
            "error",
            friendly_error(e),
            id=channel.id,
            type=channel.type,
            name=channel.name,
            category=channel_category(channel.type),
        ).to_dict()
    async with _cache_lock:
        _cache[channel.id] = (time.time() * 1000, payload)
    # 只把成功结果追加进历史趋势。用后台任务 fire-and-forget——磁盘 I/O 不应
    # 阻塞 API 响应（趋势记录是附加价值，写慢了也不该让额度查询变慢）。
    # record_result 内部已 catch 所有异常，后台任务不会抛未捕获错误。
    # 通过 _spawn_background 持有强引用，避免 Task 被 GC（裸 create_task 有此风险）。
    _spawn_background(asyncio.to_thread(history_store.record_result, channel.id, payload))
    return payload


async def _get_channel_result(channel: config_store.Channel, force: bool) -> tuple[dict, bool]:
    """单渠道查询：按 id 分别缓存（成功 60s / 失败 15s），并做请求合并——同一
    渠道正在查询时，后来的调用者等待同一个结果，而不是重复发起、并发打上游
    （比如多标签页同时打开时各自触发一次刷新）。

    返回 (结果字典, 是否直接命中缓存)。
    """
    now = time.time() * 1000
    async with _cache_lock:
        if not force:
            cached = _cache.get(channel.id)
            if cached is not None:
                cached_at, cached_result = cached
                ttl = QUOTA_CACHE_TTL_MS if cached_result.get("status") == "ok" else QUOTA_ERROR_CACHE_TTL_MS
                if now - cached_at < ttl:
                    return cached_result, True
        task = _inflight.get(channel.id)
        if task is None:
            task = asyncio.ensure_future(_query_and_cache(channel))
            _inflight[channel.id] = task

    try:
        result = await task
    finally:
        async with _cache_lock:
            if _inflight.get(channel.id) is task and task.done():
                _inflight.pop(channel.id, None)
    return result, False


@app.get("/api/quotas")
async def quotas(force: bool = False, ids: str | None = None):
    """并行查询所有渠道，返回统一结果。

    停用渠道直接标 disabled、不发网络请求；启用渠道各自按 id 缓存/合并请求；
    单个渠道异常不应该让整个接口 500（asyncio.gather 加了 return_exceptions）。

    ids：逗号分隔的渠道 id 列表，只返回/查询这些渠道（比如前端"刷新此渠道"按钮
    应该只强刷一个 id，而不是把所有已配置渠道全部打一遍上游——那正是本项目
    要按 channel id 分别缓存的意义所在）。不存在的 id 静默忽略；不传时行为和
    不加这个参数完全一致（返回全部渠道，含 disabled）。force 和 ids 正交：
    `?ids=ch_a&force=1` 只强刷 ch_a，其余渠道的缓存条目不受影响——因为缓存本来
    就按 id 分别存取，这里只是不去处理没被选中的渠道，不会主动清掉它们的缓存。
    """
    channels = config_store.list_channels()
    if ids:
        wanted = {x.strip() for x in ids.split(",") if x.strip()}
        channels = [c for c in channels if c.id in wanted]
    enabled = [c for c in channels if c.enabled]

    gathered = await asyncio.gather(*(_get_channel_result(c, force) for c in enabled), return_exceptions=True)

    by_id: dict[str, dict] = {}
    all_from_cache = True
    for channel, item in zip(enabled, gathered):
        if isinstance(item, BaseException):
            by_id[channel.id] = fail(
                "error",
                str(item) or item.__class__.__name__,
                id=channel.id,
                type=channel.type,
                name=channel.name,
                category=channel_category(channel.type),
            ).to_dict()
            all_from_cache = False
        else:
            payload, from_cache = item
            by_id[channel.id] = payload
            all_from_cache = all_from_cache and from_cache

    result_channels = []
    for c in channels:
        if not c.enabled:
            result_channels.append(_disabled_result(c))
            continue
        res = by_id.get(c.id)
        if not res or res.get("status") != "ok":
            if res:
                result_channels.append(res)
            continue
        
        # 核心逻辑：若单个渠道内部查到了多套 Plan（如火山 Agent Plan 与 Coding Plan）
        # 将它们在 Backend /api/quotas 返回层拆分为 2 个独立的 Channel/卡片对象！
        windows = res.get("windows") or []
        agent_wins = [w for w in windows if (w.get("key") or "").startswith("agent_")]
        coding_wins = [w for w in windows if (w.get("key") or "").startswith("coding_")]

        if agent_wins and coding_wins:
            # 1) Agent Plan 独立卡片
            agent_res = dict(res)
            agent_res["id"] = f"{c.id}_agent"
            agent_res["plan_name"] = "Agent Plan"
            agent_res["windows"] = agent_wins
            result_channels.append(agent_res)

            # 2) Coding Plan 独立卡片
            coding_res = dict(res)
            coding_res["id"] = f"{c.id}_coding"
            coding_res["plan_name"] = "Coding Plan"
            coding_res["windows"] = coding_wins
            result_channels.append(coding_res)
        else:
            result_channels.append(res)

    return {"cached": bool(enabled) and all_from_cache, "channels": result_channels}


# ── 本地已用统计 ─────────────────────────────────────────────


@app.get("/api/local-usage")
async def local_usage_endpoint(days: int = 14):
    """多数据源本地已用统计（目前含 Claude Code transcript + OpenCode），供
    Claude 订阅没有可用 access token 时的兜底展示，也可单独查看。"""
    days = min(max(days, 1), 90)
    return await asyncio.to_thread(local_usage.get_local_usage, days)


@app.get("/api/opencode-usage")
async def opencode_usage(days: int = 14):
    """向后兼容的薄封装：只返回 opencode 这一个数据源，扁平结构。"""
    days = min(max(days, 1), 90)
    return await asyncio.to_thread(local_usage.get_opencode_usage, days)


# ── 配置导入/导出 ─────────────────────────────────────────────


@app.get("/api/config/export")
async def export_config(include_secrets: bool = False):
    """导出当前配置。

    安全默认：include_secrets 默认 **False**（脱敏）——无认证 GET 端点默认返回明文
    密钥是危险的。需要密钥时必须显式 ?include_secrets=true。响应一律加
    Cache-Control: no-store，防止明文密钥被浏览器缓存进历史记录/磁盘缓存。
    """
    data = await asyncio.to_thread(config_store.export_config, include_secrets)
    return JSONResponse(content=data, headers={"Cache-Control": "no-store"})


@app.post("/api/config/import")
async def import_config(payload: dict, mode: Literal["merge", "replace"] = "merge"):
    """导入配置。mode=merge 追加/覆盖；mode=replace 清空后替换（危险，前端二次确认）。

    mode 用 Literal 收口：传 "Replace"（大写）、"MERGE"、拼写错误等任何非法值都会
    被 Pydantic 挡成 422→400，而不是静默当成 merge 处理（那样会让以为选了 replace
    的用户实际得到 merge，数据语义错乱且无提示）。
    """
    try:
        result = await asyncio.to_thread(config_store.import_config, payload, mode)
    except config_store.ImportConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # 导入后全量失效缓存（渠道可能新增/替换/删除，逐个失效不如清一次干净）
    async with _cache_lock:
        _cache.clear()
    return result


# ── 历史趋势 ─────────────────────────────────────────────────


@app.get("/api/history")
async def history(days: int = 30, ids: str | None = None):
    """历史趋势数据（JSONL 追加记录的成功查询结果），供前端画趋势图。

    days：返回最近 N 天（1-365，默认 30）。
    ids：逗号分隔的渠道 id 列表，只返回这些渠道；不传则返回全部已配置渠道。
    """
    days = min(max(days, 1), 365)
    wanted = {x.strip() for x in ids.split(",") if x.strip()} if ids else None
    return await asyncio.to_thread(history_store.get_history, wanted, days)


# ── 静态前端 ───────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
