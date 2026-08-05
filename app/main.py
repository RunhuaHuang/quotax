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

# 火山渠道在后端 /api/quotas 里被拆成 <id>_agent / <id>_coding 两张卡展示（前端
# 拿到的 channel id 带后缀），但 config / 缓存 / 历史都按原始 id（不带后缀）存取。
# 前端"刷新此渠道"/"停用"按钮发的 ids 会带后缀，必须归一化回 config id 才能匹配
# 到渠道。create_or_update_channel / get_channel_secret / history 四处都复用
# 下面同一个 _canonical_channel_id，逻辑必须完全一致，不能有的端点归一有的不归一。
_VOLC_PLAN_SUFFIXES = ("_agent", "_coding")


def _canonical_channel_id(channel_id: str) -> str:
    """把可能带 _agent/_coding 后缀的 channel id 归一到 config 里的真实渠道 id。

    上一版实现无条件剥掉任何 id 结尾的 _agent/_coding，完全不看这个 id 对应的
    渠道是不是火山类型——这会误伤真实存在的、id 本身就恰好以这两个词结尾的非
    火山渠道。实测复现的破坏路径：导入一个 id 为 "team_agent" 的 deepseek 渠道
    （import_config 会原样保留传入的 id），前端点"停用"发送最小 payload
    {"id":"team_agent","type":"deepseek","enabled":false}，旧逻辑把它归一成
    并不存在的 "team"，get_channel("team") 返回 None 被误判为"新建渠道"，因
    缺少必填的 api_key 而 400——这个渠道在 UI 里彻底无法编辑/停用。同理，POST
    一个新渠道、id 恰好起成 "my_coding"，会被静默存成 id "my"。

    正确做法分两步走，且顺序不能反：
    1. 先用原始 id（可能带后缀）去 config 里精确查找。只要这个 id 本身就是一个
       真实存在的渠道——不管它是不是火山、也不管它的 id 是否恰好以 _agent/
       _coding 结尾——就直接用它，不做任何改写。精确匹配永远优先，是这个函数
       最基本的不变量，也是修掉上面误伤问题的关键一步。
    2. 精确查找失败，才尝试剥掉后缀去查——但剥出来的那个 id 必须真的存在，且
       类型必须恰好是 volcengine，才采纳这个归一结果。这一步保证真实的火山子卡
       场景不被这次修复连带弄坏：config 里只存了不带后缀的原始火山渠道 id，
       前端传来的 <id>_agent/<id>_coding 就是通过这一步映射回真实渠道的。
    3. 两步都找不到，原样返回原始 id——大概率是一个确实不存在的 id，交给调用方
       走各自"渠道不存在"的正常错误路径（404 / 静默忽略等），不在这里瞎猜。
    """
    if config_store.get_channel(channel_id) is not None:
        return channel_id
    for suffix in _VOLC_PLAN_SUFFIXES:
        if channel_id.endswith(suffix):
            base_id = channel_id[: -len(suffix)]
            base_channel = config_store.get_channel(base_id)
            if base_channel is not None and base_channel.type == "volcengine":
                return base_id
    return channel_id


# 结果缓存：按渠道 id 分别缓存，成功 60s / 失败 15s（对齐 cc-switch：错误短缓存
# 以便快速重试，同时避免高频打官方接口触发风控）。之前是整体 all-or-nothing——
# 任一渠道失败就把全局 TTL 都降到 15s，导致成功的渠道也被牵连着每 15 秒重查。
QUOTA_CACHE_TTL_MS = 60_000
QUOTA_ERROR_CACHE_TTL_MS = 15_000
_cache: dict[str, tuple[float, dict]] = {}
_inflight: dict[str, asyncio.Task] = {}
_cache_lock = asyncio.Lock()

# 配置代际：每次配置变动（编辑渠道 / 导入配置）时单调递增。查询 task 在发起时
# 记录当时的代际，完成后写缓存前核对——如果代际已变（期间发生过 import_config
# 这类批量配置替换/覆盖），说明这个在飞期间发起的结果已过期（可能是旧密钥查到
# 的），丢弃它而不写入缓存，让下一次请求触发用新配置重新查询。
#
# 专门针对 import_config 的一个微妙竞态：导入覆盖某渠道密钥的瞬间，该渠道恰好有
# 一次 in-flight 查询。编辑渠道走 _invalidate_cache 会摘掉已完成的 task，但仍在
# 运行的 task 被 asyncio.shield 保护、无法摘除（摘了会孤立正在等它的其它调用者）。
# 这条用旧密钥发起的 task 跑完后原本会把旧密钥结果写回 _cache，TTL 60s 内展示
# 旧额度。代际校验让它在写缓存时发现自己已经"过时"，优雅地丢弃结果——既不取消
# 正在运行的 task（不影响其它 awaiter），也不会用旧结果污染缓存。
_generation = 0

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


# ── DNS rebinding 防护：Host 请求头白名单 ────────────────────────
#
# 背景：本服务无任何认证，只监听 127.0.0.1，但 GET /api/config/export?
# include_secrets=true 会返回明文 API Key（config_store.export_config 的
# docstring 早就点名了这个风险，但一直没有实际防护）。仅仅"监听在 127.0.0.1"
# 不是安全边界：一个恶意网页可以用 DNS rebinding 攻击——先用一个攻击者控制的
# 域名（首次 DNS 解析到攻击者自己的服务器，通过浏览器的初始连接/证书检查），
# 再把这个域名的 DNS 记录（配合很短的 TTL）改指向 127.0.0.1，诱导浏览器重新
# 解析；后续从同一个页面发出的 fetch/XHR 请求会被发到本机这个服务上，而浏览器
# 仍然认为这是"同源"请求（域名字符串一直没变，变的只是它背后指向的 IP，不受
# 同源策略拦截）。
#
# 为什么校验 Host 头有效：Host 是浏览器根据请求 URL 的 authority 部分自动
# 填写的"禁止头"（forbidden header name，Fetch 规范明确禁止 JS 通过 fetch/XHR
# 显式设置或覆盖它）。即使 DNS 把 evil.com 解析到了 127.0.0.1，浏览器发出的
# 请求 Host 头仍然是 "evil.com"（或 "evil.com:<port>"），不会变成
# "127.0.0.1"——所以只要挡住 Host 头不在白名单里的请求，就能挡住这类攻击。
_ALLOWED_HOSTS = {
    "127.0.0.1",
    "localhost",
    "::1",
    # Starlette TestClient（httpx 的 ASGITransport）默认发送的 Host 头固定是
    # "testserver"，测试代码不会也不需要显式设置它。本项目 100+ 个测试全部走
    # TestClient，不放行这个值的话会全部返回 403。这不是需要额外环境变量开关
    # 才能豁免的生产风险："testserver" 是一个不含点的单标签名，公网 DNS 无法
    # 把它解析到攻击者的服务器，残余风险仅限于用户自己在本地 hosts/内网 DNS 里
    # 把这个名字配置指向本机的极端场景，和本工具"个人本机使用"的定位不冲突。
    "testserver",
}


def _host_without_port(host_header: str) -> str:
    """从 Host 请求头里剥掉端口号（以及 IPv6 字面量的方括号），只留主机名/IP。

    服务可能起在任意端口（README 里就有 8900/8931 等不同示例，测试也会用别的
    端口），白名单不能写死端口，只需要校验冒号前的主机部分——不管请求实际连的
    是哪个端口。
    """
    value = host_header.strip().lower()
    if value.startswith("["):
        # IPv6 字面量形如 "[::1]" 或 "[::1]:8900"——地址本身包含多个冒号，不能
        # 简单按冒号切分，要用配对的 "]" 定位地址边界。
        end = value.find("]")
        return value[1:end] if end != -1 else value
    # IPv4 / 主机名：最多带一个 ":<port>" 后缀（主机名和 IPv4 地址本身都不含
    # 冒号），直接按最后一个冒号切一次即可。
    return value.rsplit(":", 1)[0] if ":" in value else value


@app.middleware("http")
async def _enforce_host_whitelist(request: Request, call_next):
    """DNS rebinding 防护：Host 头不在白名单里一律 403，不进入任何业务路由。

    中间件按 Host 头判断，不看请求路径——静态文件（/static/...）和首页（/）
    在合法 Host 下会照常通过，不需要针对路径单独放行；本地实测已确认 GET /、
    /static/app.js、/api/health 在合法 Host 下均为 200（见部署验证脚本）。
    """
    if _host_without_port(request.headers.get("host", "")) not in _ALLOWED_HOSTS:
        host_header = request.headers.get("host") or "(空)"
        return JSONResponse(
            status_code=403,
            content={"detail": f"非法的请求来源（Host: {host_header}），出于防 DNS rebinding 攻击考虑仅允许本机访问"},
        )
    return await call_next(request)


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
    # 这些后缀。停用/启用需要归一回真实 config id（不带后缀），否则会被当成新建；
    # 但只有精确匹配失败、且剥出来的 id 真的是 volcengine 渠道时才会被改写——见
    # _canonical_channel_id 的文档字符串，普通渠道 id 恰好以 _agent/_coding
    # 结尾的情况不会被误伤。
    if payload.id:
        payload.id = _canonical_channel_id(payload.id)

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
        # 清理上传的 Codex 凭据文件（私有文件，渠道删除后没有保留的必要）。
        # codex_auth_file 经 resolve_codex_auth_file 校验——extra 是用户可自由设置
        # 的字段，不校验的话 ../../ 或绝对路径会让 unlink 删除本目录之外的任意文件。
        cred_path = config_store.resolve_codex_auth_file(channel.extra["codex_auth_file"])
        if cred_path is not None:
            try:
                cred_path.unlink(missing_ok=True)
            except OSError:
                pass
    return {"ok": True}


@app.get("/api/channels/{channel_id}/secret")
async def get_channel_secret(channel_id: str):
    """返回指定渠道的明文密钥（仅本机无认证访问，供编辑表单"显示密钥"使用）。"""
    # 火山子渠道 id 带 _agent/_coding 后缀，归一到真实 config id（只在精确匹配
    # 失败、且剥出来的 id 确实是 volcengine 渠道时才会被改写，见函数文档）。
    base_id = _canonical_channel_id(channel_id)
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
    global _generation
    async with _cache_lock:
        _generation += 1
        _cache.pop(channel_id, None)
        # 编辑渠道（如换密钥）后应真正重新查询，而不是复用一个还在 _inflight 里、
        # 早已完成的旧 task 的旧结果。只摘除已完成的：仍在运行的 task 不能动（否则
        # 会孤立正在后台跑的查询、让等待它的调用者拿不到结果）。
        inflight = _inflight.get(channel_id)
        if inflight is not None and inflight.done():
            _inflight.pop(channel_id, None)


def _disabled_result(channel: config_store.Channel) -> dict:
    """停用渠道：不发起任何网络请求，直接标记 status="disabled"。"""
    return ChannelResult(
        id=channel.id,
        type=channel.type,
        name=channel.name,
        category=channel_category(channel.type),
        status="disabled",
    ).to_dict()


def split_multi_plan_result(channel: config_store.Channel, res: dict) -> list[dict]:
    """把单个渠道查询结果里"同时查到多套 Plan"的情况拆成多张卡片。

    目前唯一的多 Plan 场景是火山方舟：同一账号同时开了 Agent Plan + Coding Plan，
    provider 层把两套套餐的窗口放在同一个 ChannelResult.windows 里（key 带
    agent_ / coding_ 前缀）。这里按前缀把它们拆成两张独立卡片，各自带独立 id
    （<id>_agent / <id>_coding）、独立 plan_name（取 extra 里 provider 带出的真实
    套餐名）、各自的窗口子集。

    抽成独立函数是为了让 CLI（app/cli.py 的 _fetch_quotas）和 Web 端（/api/quotas）
    复用同一套拆分逻辑——README 明确承诺 `quotaboard quota --json` 的 channels
    数组结构与 `GET /api/quotas` 完全一致，两处必须走同一个函数，否则火山双套餐
    渠道在 CLI 里会少一条、id 不带后缀、windows 没分桶，脚本无法复用同一套解析。

    不满足拆分条件（没有 agent_/coding_ 前缀的窗口，或只有其一）时原样返回单条。
    """
    windows = res.get("windows") or []
    agent_wins = [w for w in windows if (w.get("key") or "").startswith("agent_")]
    coding_wins = [w for w in windows if (w.get("key") or "").startswith("coding_")]
    if not (agent_wins and coding_wins):
        return [res]
    # plan_name 取 provider 层通过 extra 带出的、每个套餐各自的真实名称（如
    # "火山 Agent Plan small"，含 PlanType 档位——见 volcengine._merge_plans 的
    # 文档字符串）；extra 里没有对应 key 时退回通用兜底文案，不至于没有 plan_name。
    plan_names = res.get("extra") or {}
    agent_res = dict(res)
    agent_res["id"] = f"{channel.id}_agent"
    agent_res["plan_name"] = plan_names.get("agent_plan_name") or "Agent Plan"
    agent_res["windows"] = agent_wins
    coding_res = dict(res)
    coding_res["id"] = f"{channel.id}_coding"
    coding_res["plan_name"] = plan_names.get("coding_plan_name") or "Coding Plan"
    coding_res["windows"] = coding_wins
    return [agent_res, coding_res]


async def _query_and_cache(channel: config_store.Channel, gen: int) -> dict:
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
        # 代际校验：task 发起到完成期间若发生过配置变动（import_config 覆盖密钥、
        # 编辑渠道等），_generation 已被 bump，此时这条结果（可能用旧密钥查到）
        # 已过期，丢弃它不写缓存——下一次请求缓存未命中会触发新查询。代际仍一致
        # 才写入，保证缓存的永远是当前配置下的结果。
        if gen == _generation:
            _cache[channel.id] = (time.time() * 1000, payload)
    # 只把成功结果追加进历史趋势。用后台任务 fire-and-forget——磁盘 I/O 不应
    # 阻塞 API 响应（趋势记录是附加价值，写慢了也不该让额度查询变慢）。
    # record_result 内部已 catch 所有异常，后台任务不会抛未捕获错误。
    # 通过 _spawn_background 持有强引用，避免 Task 被 GC（裸 create_task 有此风险）。
    # 注意：代际过期的结果仍然记历史——它确实是某个时刻真实查到的数据点，只是
    # 不该占用"当前配置下最新结果"的缓存槽位。
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
        # 复用 _inflight 里的 task 前，必须先检查它是否已经 done。
        # 否则会发生一个微妙的脏数据路径：某次请求的 awaiter 在 task 跑完前被取消
        # （客户端断连），finally 里观察到 task.done()=False 不摘除它；task 之后在
        # 后台跑完、写进 _cache，但 _inflight[id] 仍指向这个已完成 task。等下次缓存
        # 失效（编辑渠道 / TTL 到期）再查，缓存未命中 → 拿到这个旧 task → 直接返回
        # 它当初的结果，而不是发起新查询（force=True 也绕不过这条复用路径）。done
        # 检查让已完成的残留 task 被当成"没有在飞"对待，从而新建一个真正的新查询。
        task = _inflight.get(channel.id)
        if task is None or task.done():
            # 捕获发起时的配置代际：task 完成写缓存前用它核对，期间若发生过
            # import_config / 编辑渠道（_generation 被 bump），这条结果视为已过期
            # 而丢弃，不污染缓存（见 _query_and_cache 的代际校验）。
            task = asyncio.ensure_future(_query_and_cache(channel, _generation))
            _inflight[channel.id] = task

    try:
        # asyncio.shield：多个并发调用者可能在 await 同一个共享 task（见上面的
        # 请求合并逻辑）。如果这里直接 `await task`，一旦*任意一个*调用者的外层
        # 协程被取消（比如客户端断连、uvicorn 取消了这一次请求处理），asyncio
        # 的语义是取消会顺着 await 传播进被 await 的 task——那会把这个共享的
        # 查询 task 也取消掉，连累其他仍在等待同一个结果、客户端根本没断开的
        # 调用者（它们的 await task 会跟着抛 CancelledError，而不是拿到正确
        # 结果）。asyncio.shield(task) 隔离了这一点：外层取消只会取消 shield()
        # 返回的这一层 wrapper（下面这个 await 抛 CancelledError），不会把 task
        # 本身取消掉——task 会继续在后台跑完、正常写入 _cache，其它调用者不受
        # 影响。
        result = await asyncio.shield(task)
    finally:
        # 推演 shield 语义下这个清理条件是否依然正确：
        # 外层被取消时，上面的 await 在 task 还没跑完的情况下抛 CancelledError，
        # 直接进入这个 finally——此时 task 仍在后台运行（没有被 shield 取消），
        # task.done() 几乎总是 False，所以下面的条件不成立、不会把它从 _inflight
        # 摘掉。这正是我们想要的：这个 task 仍然"在飞"，其它并发调用者、乃至
        # 后续新来的请求都应该继续找到它、等它，而不能误以为它已经结束。只有
        # 真正观察到 task.done()（正常完成，或者 task 自己内部异常/被直接
        # cancel）的那次 await 才会执行清理；`_inflight.get(channel.id) is task`
        # 这个身份比较保证了即使清理发生得晚，也不会误删一个后来者刚为同一个
        # channel_id 创建的、对象不同的新 task。
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
        # 归一化 _agent/_coding 后缀：火山渠道在 /api/quotas 返回层被拆成两张卡
        # （id 带 _agent/_coding），前端"刷新此渠道"按钮发的 ids 会带后缀，必须
        # 归一回 config id 才能匹配到渠道，否则单卡刷新火山子卡时返回空、卡上的
        # 数据永远更新不了。
        wanted = {_canonical_channel_id(x.strip()) for x in ids.split(",") if x.strip()}
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

        # 火山方舟同一账号可能同时查到 Agent Plan + Coding Plan，拆成两张独立卡片。
        # 这段拆分逻辑抽成了 split_multi_plan_result，CLI 的 _fetch_quotas 也复用
        # 同一个函数，保证 `quotaboard quota --json` 与 `GET /api/quotas` 结构一致。
        result_channels.extend(split_multi_plan_result(c, res))

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
    # 导入后全量失效缓存（渠道可能新增/替换/删除，逐个失效不如清一次干净）。
    # bump _generation：导入前已在飞的查询 task（用旧配置/旧密钥发起）完成后会
    # 发现代际已变，丢弃结果不写缓存——否则这些旧 task 跑完会把旧密钥查到的额度
    # 写回 _cache，TTL 60s 内展示的是导入前的旧数据。仍在运行的 task 这里仍然不
    # 取消（取消会孤立正在 await 它的其它调用者），靠代际校验让它的结果自然失效。
    # 同时摘除 _inflight 里已完成的残留 task，避免导入后复用旧 task。
    global _generation
    async with _cache_lock:
        _generation += 1
        _cache.clear()
        for cid in [cid for cid, t in _inflight.items() if t.done()]:
            _inflight.pop(cid, None)
    return result


# ── 历史趋势 ─────────────────────────────────────────────────


@app.get("/api/history")
async def history(days: int = 30, ids: str | None = None):
    """历史趋势数据（JSONL 追加记录的成功查询结果），供前端画趋势图。

    days：返回最近 N 天（1-365，默认 30）。
    ids：逗号分隔的渠道 id 列表，只返回这些渠道；不传则返回全部已配置渠道。

    与 create_or_update_channel / get_channel_secret / quotas 三处一样，这里也
    要用 _canonical_channel_id 归一 _agent/_coding 后缀——这四个端点都会接到
    "渠道 id"，必须是同一套归一逻辑，不能三个做归一、剩这一个不做（目前前端
    历史趋势下拉用的是不带后缀的配置 id，这个不一致还没被触发暴露，但如果哪天
    前端改成传火山子卡的带后缀 id，不归一就会查不到对应渠道的历史）。
    """
    days = min(max(days, 1), 365)
    wanted = {_canonical_channel_id(x.strip()) for x in ids.split(",") if x.strip()} if ids else None
    return await asyncio.to_thread(history_store.get_history, wanted, days)


# ── 静态前端 ───────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
