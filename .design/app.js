/* ── QuotaX 前端逻辑 ─────────────────────────────────── */

const $ = (sel) => document.querySelector(sel);

const CATEGORY_ORDER = ["balance", "coding_plan", "subscription", "local"];

/* 编辑渠道时需要"留空表示不修改"的密钥字段 */
const SECRET_FIELDS = ["api_key", "ak", "sk"];

/* 本地用量统计默认拉取的天数（展示文案用后端返回的真实 days，这里只是请求参数） */
const LOCAL_USAGE_DAYS = 14;

/* 各渠道的视觉元数据 */
const PROVIDER_META = {
  deepseek:            { icon: "D", a: "#4d6bfe", b: "#2b3fb3", cat: "balance" },
  stepfun:             { icon: "阶", a: "#7c3aed", b: "#4c1d95", cat: "balance" },
  siliconflow:         { icon: "硅", a: "#0ea5e9", b: "#075985", cat: "balance" },
  openrouter:          { icon: "OR", a: "#f59e0b", b: "#b45309", cat: "balance" },
  novita:              { icon: "N", a: "#ec4899", b: "#9d174d", cat: "balance" },
  kimi_api:            { icon: "K", a: "#3b82f6", b: "#1e40af", cat: "balance" },
  newapi:              { icon: "中", a: "#10b981", b: "#065f46", cat: "balance" },
  kimi_coding:         { icon: "K", a: "#3b82f6", b: "#1e40af", cat: "coding_plan" },
  zhipu_coding:        { icon: "智", a: "#6366f1", b: "#3730a3", cat: "coding_plan" },
  zhipu_team:          { icon: "团", a: "#8b5cf6", b: "#5b21b6", cat: "coding_plan" },
  minimax:             { icon: "M", a: "#22d3ee", b: "#0e7490", cat: "coding_plan" },
  volcengine:          { icon: "火", a: "#f43f5e", b: "#9f1239", cat: "coding_plan" },
  zenmux:              { icon: "Z", a: "#a3e635", b: "#4d7c0f", cat: "coding_plan" },
  mimo:                { icon: "米", a: "#ff6900", b: "#c2410c", cat: "coding_plan" },
  claude_subscription: { icon: "Cl", a: "#d97757", b: "#9a3412", cat: "subscription" },
  gemini_subscription: { icon: "G", a: "#4285f4", b: "#1a3a8f", cat: "subscription" },
  grok_subscription:   { icon: "G", a: "#94a3b8", b: "#475569", cat: "subscription" },
  codex_subscription:  { icon: "C", a: "#10a37f", b: "#0b5c48", cat: "subscription" },
  copilot_subscription:{ icon: "GH", a: "#8b949e", b: "#30363d", cat: "subscription" },
};

const FALLBACK_META = { icon: "?", a: "var(--accent)", b: "var(--accent-2)", cat: "balance" };

const STATUS_TEXT = {
  ok: "正常", info: "仅提示", expired: "已过期", error: "错误", not_found: "未登录", disabled: "已停用",
};
const STATUS_TAG_CLASS = {
  ok: "ok", info: "info", expired: "expired", error: "error", not_found: "not_found", disabled: "disabled",
};

let providersCatalog = {};
let categories = {};
let channels = [];
let quotas = [];
let localUsage = null;
let editingId = null;
let autoRefreshTimer = null;
let dashboardLoadedOnce = false;

/* ── 用户偏好（持久化到 localStorage）───────────────────────── */

const PREFS_KEY = "quotaboard_prefs";

/* 自动刷新间隔（毫秒）。默认 90 秒——故意略大于后端成功缓存 TTL（60s），
   否则间隔正好等于缓存 TTL 时，定时器几乎永远命中缓存拿不到新数据
   （缓存写入时刻与定时器起点不同步，< 的严格比较在边界上很脆弱）。
   90s 既保证定时刷新能拿到真正的新数据，又不会太频繁打扰上游。
   用户可在设置里改成 30/60/90/180/300 秒。 */
const REFRESH_INTERVALS = [
  { value: 0, label: "关闭" },
  { value: 30_000, label: "30 秒" },
  { value: 60_000, label: "1 分钟" },
  { value: 90_000, label: "90 秒" },
  { value: 180_000, label: "3 分钟" },
  { value: 300_000, label: "5 分钟" },
];
const DEFAULT_REFRESH_INTERVAL = 90_000;

/* 主题：auto = 跟随系统；light / dark 手动覆盖 */
const THEME_OPTIONS = [
  { value: "auto", label: "跟随系统" },
  { value: "light", label: "浅色" },
  { value: "dark", label: "深色" },
];

function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
  } catch {
    return {};
  }
}

function savePrefs(patch) {
  try {
    const merged = { ...loadPrefs(), ...patch };
    localStorage.setItem(PREFS_KEY, JSON.stringify(merged));
  } catch {
    /* localStorage 不可用（隐私模式/配额满）——静默降级，不阻断核心功能 */
  }
}

/* ── 初始化 ─────────────────────────────────────────────── */

async function init() {
  renderSkeletonState();
  bindEvents();
  applyStoredTheme(); // 尽早应用主题，避免首屏闪烁
  try {
    await loadProviders();
  } catch (e) {
    console.error(e);
    renderFatalError(`加载渠道类型失败（/api/providers）：${e.message}。请确认后端服务已启动，然后重试。`);
    return;
  }
  await Promise.all([loadChannels(), refreshQuotas(true), loadLocalUsage()]);
  setupAutoRefresh();
  setupVisibilityAutoRefresh();
  setupThemeReactivity();
}

function bindEvents() {
  $("#btnRefresh").addEventListener("click", () => refreshQuotas(true));
  $("#btnConfig").addEventListener("click", openConfigModal);
  $("#btnCloseModal").addEventListener("click", closeConfigModal);
  $("#configModal").addEventListener("click", (e) => {
    if (e.target === $("#configModal")) closeConfigModal();
  });
  $("#btnFormReset").addEventListener("click", resetForm);
  $("#channelForm").addEventListener("submit", onSaveChannel);
  $("#fType").addEventListener("change", () => renderDynamicFields());
  // 密钥输入框的显示/隐藏切换（事件委托：字段是动态渲染的）
  $("#dynamicFields").addEventListener("click", (e) => {
    const btn = e.target.closest(".pw-toggle");
    if (!btn) return;
    const input = btn.parentElement.querySelector("input");
    if (!input) return;
    const isPw = input.type === "password";
    input.type = isPw ? "text" : "password";
    btn.classList.toggle("active", isPw);
  });
  $("#autoRefresh").addEventListener("change", (e) => {
    if (e.target.checked) setupAutoRefresh();
    else clearInterval(autoRefreshTimer);
  });

  // 设置弹窗
  $("#btnSettings").addEventListener("click", openSettingsModal);
  $("#btnCloseSettings").addEventListener("click", closeSettingsModal);
  $("#settingsModal").addEventListener("click", (e) => {
    if (e.target === $("#settingsModal")) closeSettingsModal();
  });

  // 历史趋势弹窗
  $("#btnHistory").addEventListener("click", openHistoryModal);
  $("#btnCloseHistory").addEventListener("click", closeHistoryModal);
  $("#historyModal").addEventListener("click", (e) => {
    if (e.target === $("#historyModal")) closeHistoryModal();
  });
  $("#historyDays").addEventListener("change", loadHistory);
  $("#historyChannel").addEventListener("change", loadHistory);

  // 配置导入/导出
  $("#btnExportSecret").addEventListener("click", () => exportConfig(true));
  $("#btnExportSafe").addEventListener("click", () => exportConfig(false));
  $("#btnImportTrigger").addEventListener("click", () => $("#importFileInput").click());
  $("#importFileInput").addEventListener("change", onImportFileSelected);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeConfigModal(); closeSettingsModal(); closeHistoryModal(); }
  });

  // 仪表盘：事件委托（避免内联 onclick，卡片会随渠道数据频繁重绘）
  $("#dashboard").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;
    if (action === "open-config") openConfigModal();
    else if (action === "retry-init") location.reload();
    else if (action === "retry-refresh") refreshQuotas(true);
    else if (action === "refresh-one") onRefreshOneCard(btn);
  });
  $("#dashboard").addEventListener("change", (e) => {
    const el = e.target.closest('[data-action="toggle-enabled"]');
    if (el) onToggleEnabled(el);
  });

  // 配置弹窗内的渠道列表：同样走事件委托
  $("#channelsList").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const id = btn.dataset.id;
    if (btn.dataset.action === "edit-channel") {
      const ch = channels.find((c) => c.id === id);
      if (ch) fillForm(ch);
    } else if (btn.dataset.action === "delete-channel") {
      deleteChannel(id);
    }
  });
}

/* ── 数据加载 ───────────────────────────────────────────── */

async function loadProviders() {
  const res = await fetch("/api/providers");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  providersCatalog = data.providers || {};
  categories = data.categories || {};
}

async function loadChannels() {
  try {
    const res = await fetch("/api/channels");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    channels = await res.json();
    renderChannelsList();
    $("#channelCount").textContent = channels.length;
  } catch (e) {
    toast("加载渠道列表失败: " + e.message, "err");
  }
}

async function refreshQuotas(force = false) {
  const btn = $("#btnRefresh");
  btn.classList.add("spinning");
  btn.setAttribute("aria-busy", "true");
  try {
    const url = force ? "/api/quotas?force=1" : "/api/quotas";
    const data = await fetchQuotas(url);
    quotas = data.channels;
    dashboardLoadedOnce = true;
    renderDashboard();
    const now = new Date();
    $("#lastUpdated").textContent = `上次刷新 ${now.toLocaleTimeString("zh-CN", { hour12: false })}${data.cached ? " · 命中缓存" : ""}`;
  } catch (e) {
    toast("刷新额度失败" + (dashboardLoadedOnce ? "，已保留上次数据" : "") + ": " + e.message, "err");
    if (!dashboardLoadedOnce) {
      // 从未成功加载过：给出明确的错误态，不能伪装成"还没配置渠道"的空状态
      renderFatalDashboardError(e.message);
    }
  } finally {
    btn.classList.remove("spinning");
    btn.removeAttribute("aria-busy");
  }
}

async function fetchQuotas(url) {
  const res = await fetch(url);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const err = await res.json();
      if (err && err.detail) detail = err.detail;
    } catch { /* 响应体不是 JSON，用状态码兜底 */ }
    throw new Error(detail);
  }
  const data = await res.json();
  data.channels = Array.isArray(data.channels) ? data.channels : [];
  return data;
}

async function loadLocalUsage() {
  try {
    const res = await fetch(`/api/local-usage?days=${LOCAL_USAGE_DAYS}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    localUsage = await res.json();
    renderLocalUsage();
  } catch (e) {
    localUsage = null;
    $("#localUsage").classList.add("hidden");
    $("#localUsage").innerHTML = "";
    toast("本地用量统计加载失败: " + e.message, "err");
  }
}

function getRefreshInterval() {
  const prefs = loadPrefs();
  const v = prefs.refreshInterval;
  // REFRESH_INTERVALS 里没有的值（含 NaN/null/字符串）一律回退默认，避免坏值把
  // 定时器设成 0 或负数导致死循环刷新。
  return typeof v === "number" && REFRESH_INTERVALS.some((o) => o.value === v) ? v : DEFAULT_REFRESH_INTERVAL;
}

function setupAutoRefresh() {
  clearInterval(autoRefreshTimer);
  const interval = getRefreshInterval();
  if ($("#autoRefresh").checked && !document.hidden && interval > 0) {
    autoRefreshTimer = setInterval(() => refreshQuotas(false), interval);
  }
}

/* 页面隐藏时暂停轮询，重新可见时立即刷新一次并恢复定时 */
function setupVisibilityAutoRefresh() {
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearInterval(autoRefreshTimer);
    } else if ($("#autoRefresh").checked) {
      refreshQuotas(false);
      setupAutoRefresh();
    }
  });
}

/* 系统深色/浅色模式切换时，重绘一次以让 ringColor() 等运行时取色的地方生效。
   renderDashboard() 内部会在"从未成功加载过"时直接跳过（见其内的 dashboardLoadedOnce 判断），
   避免主题切换把 skeleton / 致命错误态覆盖成"还没有配置任何渠道"的空状态。 */
function setupThemeReactivity() {
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  mq.addEventListener("change", () => {
    renderDashboard();
    renderLocalUsage();
  });
}

/* ── 仪表盘渲染 ─────────────────────────────────────────── */

function renderSkeletonState() {
  const skCard = () => `
    <div class="card loading">
      <div class="card-head">
        <div class="skeleton sk-icon"></div>
        <div class="card-title" style="flex:1">
          <div class="skeleton sk-line" style="width:65%"></div>
          <div class="skeleton sk-line" style="width:40%;height:9px;margin-top:6px;margin-bottom:0"></div>
        </div>
      </div>
      <div class="skeleton sk-big"></div>
      <div class="sk-row">
        <div class="skeleton sk-ring"></div>
        <div class="skeleton sk-ring"></div>
      </div>
    </div>`;
  $("#dashboard").innerHTML = `<div class="grid">${Array.from({ length: 6 }, skCard).join("")}</div>`;
}

function renderFatalError(message) {
  $("#dashboard").innerHTML = `
    <div class="empty-state error-state">
      <div class="empty-icon">⚠️</div>
      <h3>页面初始化失败</h3>
      <p>${esc(message)}</p>
      <button type="button" class="btn btn-primary" data-action="retry-init">重新加载页面</button>
    </div>`;
}

function renderFatalDashboardError(message) {
  $("#dashboard").innerHTML = `
    <div class="empty-state error-state">
      <div class="empty-icon">⚠️</div>
      <h3>额度数据加载失败</h3>
      <p>${esc(message || "请检查后端服务是否正常运行。")}</p>
      <button type="button" class="btn btn-primary" data-action="retry-refresh">重试</button>
    </div>`;
}

function renderDashboard() {
  // 从未成功加载过额度数据（还在 skeleton，或首次加载就失败了）：不要动 DOM。
  // 否则主题切换等触发的重绘会把"额度数据加载失败 + 重试"覆盖成"还没有配置任何渠道"，
  // 把一个真实的后端故障伪装成空状态。
  if (!dashboardLoadedOnce) return;
  refreshThemeColorCache();
  const container = $("#dashboard");
  renderSummaryChips(quotas);

  if (quotas.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📊</div>
        <h3>还没有配置任何渠道</h3>
        <p>点击右上角「配置渠道」添加你的余额与订阅渠道。订阅类渠道（Claude / Gemini / Grok / Codex / Copilot）无需填写任何密钥，自动读取本机 CLI 的登录状态。</p>
        <button type="button" class="btn btn-primary" data-action="open-config">＋ 添加第一个渠道</button>
      </div>`;
    return;
  }

  // 按分类分组（包含已停用渠道，以弱化样式显示在原分类下）
  const groups = {};
  for (const q of quotas) {
    (groups[q.category] ||= []).push(q);
  }

  // 已知分类按固定顺序展示，未知分类（防御性：后端以后新增分类时不至于让渠道悄悄消失）追加在后面
  const knownCats = CATEGORY_ORDER.filter((cat) => groups[cat]);
  const extraCats = Object.keys(groups).filter((cat) => !CATEGORY_ORDER.includes(cat));

  container.innerHTML = [...knownCats, ...extraCats]
    .map((cat) => {
      const items = groups[cat].map(renderCard).join("");
      return `
        <section class="section">
          <div class="section-head">
            <h2><span class="section-dot ${esc(cat)}"></span>${esc(categories[cat] || cat)}</h2>
            <span class="section-note">${groups[cat].length} 个渠道</span>
          </div>
          <div class="grid">${items}</div>
        </section>`;
    })
    .join("");
}

function renderSummaryChips(list) {
  const ok = list.filter((q) => q.status === "ok").length;
  const info = list.filter((q) => q.status === "info").length;
  const warn = list.filter((q) => q.status === "error" || q.status === "not_found").length;
  const bad = list.filter((q) => q.status === "expired").length;
  const off = list.filter((q) => q.status === "disabled").length;
  const low = list.filter(channelBreachesThreshold).length; // 低余额阈值告警
  $("#summaryChips").innerHTML = `
    <span class="chip ok"><span class="chip-dot"></span>正常 <b>${ok}</b></span>
    ${low ? `<span class="chip low"><span class="chip-dot"></span>低额度 <b>${low}</b></span>` : ""}
    ${info ? `<span class="chip info"><span class="chip-dot"></span>提示 <b>${info}</b></span>` : ""}
    ${warn ? `<span class="chip warn"><span class="chip-dot"></span>异常 <b>${warn}</b></span>` : ""}
    ${bad ? `<span class="chip bad"><span class="chip-dot"></span>过期 <b>${bad}</b></span>` : ""}
    ${off ? `<span class="chip off"><span class="chip-dot"></span>停用 <b>${off}</b></span>` : ""}`;
}

function renderCard(q) {
  const meta = PROVIDER_META[q.type] || FALLBACK_META;
  const statusCls = STATUS_TAG_CLASS[q.status] || "not_found";
  const statusText = STATUS_TEXT[q.status] || q.status;
  const isDisabled = q.status === "disabled";
  const isAbnormal = !(q.status === "ok" || q.status === "info");
  const isLowAlert = channelBreachesThreshold(q); // 低余额阈值告警
  const typeLabel = esc(providersCatalog[q.type]?.label || q.type);

  let body = "";
  if (q.status === "ok") {
    const parts = [];
    if (q.amount) {
      parts.push(`
        <div class="card-amount">
          <span class="value">${esc(q.amount.label)}</span>
          <span class="currency">${esc(q.amount.currency || "")}</span>
        </div>`);
    }
    if (q.windows && q.windows.length) {
      parts.push(renderWindows(q.windows));
    }
    if (!parts.length) {
      parts.push(`<div class="card-amount"><span class="value placeholder">暂无数据</span></div>`);
    }
    body = parts.join("");
  } else if (q.status === "info") {
    // info：渠道可用但没有可查的额度数据（如未取到明文 token）——中性提示，绝不能长得像 100% 满额度
    body = `<div class="card-info-msg">${esc(q.message || "该渠道当前没有可查询的额度数据。")}</div>`;
  } else if (isDisabled) {
    body = `<div class="card-amount"><span class="value placeholder">已停用，不参与查询</span></div>`;
  } else {
    body = `<div class="card-amount"><span class="value placeholder">暂无数据</span></div>`;
  }

  // 底部：正常/提示状态优先展示凭据来源；异常状态展示错误信息。两者都有时互为提示 title。
  let footText, footTitle, footCls;
  if (!isAbnormal) {
    footText = q.source || q.message || "";
    footTitle = q.source && q.message ? `${q.source}\n${q.message}` : footText;
    footCls = "src";
  } else {
    footText = q.message || statusText;
    footTitle = q.source ? `${footText}\n来源：${q.source}` : footText;
    footCls = "msg";
  }
  const time = q.updated_at ? fmtTime(q.updated_at) : "";

  const showRefresh = !isDisabled;
  const actions = `
    <div class="card-actions">
      ${showRefresh ? `
      <button type="button" class="btn-icon card-action" data-action="refresh-one" data-id="${esc(q.id)}" title="刷新此渠道">
        <svg viewBox="0 0 24 24" class="icon"><path fill="currentColor" d="M17.65 6.35A8 8 0 1 0 19.73 14h-2.08a6 6 0 1 1-1.42-5.93L13 11h7V4l-2.35 2.35z"/></svg>
      </button>` : ""}
      <label class="switch switch-sm" title="${isDisabled ? "启用此渠道" : "停用此渠道"}">
        <input type="checkbox" data-action="toggle-enabled" data-id="${esc(q.id)}" data-type="${esc(q.type)}" ${isDisabled ? "" : "checked"}>
        <span class="switch-track"><span class="switch-thumb"></span></span>
      </label>
    </div>`;

  return `
    <article class="card${isDisabled ? " is-disabled" : ""}${isLowAlert ? " is-low-alert" : ""}" style="--card-accent:${meta.a}" data-id="${esc(q.id)}">
      <div class="card-head">
        <div class="icon-tile" style="--tile-a:${meta.a};--tile-b:${meta.b}">${esc(meta.icon)}</div>
        <div class="card-title">
          <div class="name">${esc(q.name)}</div>
          <div class="plan">${q.plan_name ? esc(q.plan_name) : typeLabel}</div>
        </div>
        <span class="status-dot ${statusCls}" title="${esc(footTitle || statusText)}"></span>
        <span class="status-tag ${statusCls}">${esc(statusText)}</span>
      </div>
      ${body}
      <div class="card-foot">
        <span class="${footCls}" title="${esc(footTitle)}">${esc(footText)}</span>
        <span class="time">${esc(time)}</span>
        ${actions}
      </div>
    </article>`;
}

function renderWindows(windows) {
  if (windows.length <= 4) {
    return `<div class="windows">${windows.map(renderRing).join("")}</div>`;
  }
  return `<div>${windows.map(renderBar).join("")}</div>`;
}

/* 两个百分比字段都缺失：这个条目只是文本标签（如"充值余额""订阅计划"），没有百分比概念 */
function noPercentData(w) {
  const noUsed = w.used_percent === null || w.used_percent === undefined;
  const noRemaining = w.remaining_percent === null || w.remaining_percent === undefined;
  return noUsed && noRemaining;
}

/* ringColor() 在 renderRing/renderBar 里逐个窗口调用（N 张卡 × M 个窗口），
   如果每次都 getComputedStyle() 会是大量强制样式读取。这里改成每轮渲染只读一次、
   缓存到 themeColorCache，renderDashboard() 开头负责刷新它（自然也覆盖了主题切换的场景）。 */
let themeColorCache = null;

function refreshThemeColorCache() {
  const style = getComputedStyle(document.documentElement);
  themeColorCache = {
    accent: style.getPropertyValue("--accent").trim(),
    green: style.getPropertyValue("--green").trim(),
    amber: style.getPropertyValue("--amber").trim(),
    red: style.getPropertyValue("--red").trim(),
  };
}

function ringColor(remainingPct) {
  if (!themeColorCache) refreshThemeColorCache(); // 防御性兜底：理论上 renderDashboard() 总会先刷新一次
  if (remainingPct === null || remainingPct === undefined) return themeColorCache.accent;
  if (remainingPct >= 60) return themeColorCache.green;
  if (remainingPct >= 30) return themeColorCache.amber;
  return themeColorCache.red;
}

function renderRing(w) {
  if (noPercentData(w)) return renderRingFlat(w);
  const C = 2 * Math.PI * 26;
  const remaining = w.remaining_percent ?? (100 - w.used_percent);
  const pct = Math.max(0, Math.min(100, remaining));
  const offset = C * (1 - pct / 100);
  const color = ringColor(pct);
  const reset = w.reset_at ? ` · <span class="window-reset">${fmtReset(w.reset_at)}</span>` : "";
  const sub = w.max_label ? `<span class="ring-sub">${esc(w.max_label)}</span>` : "";
  return `
    <div class="window-ring">
      <div class="ring-wrap">
        <svg viewBox="0 0 64 64">
          <circle class="ring-bg" cx="32" cy="32" r="26" fill="none" stroke-width="6"/>
          <circle class="ring-fg" cx="32" cy="32" r="26" fill="none" stroke-width="6"
                  stroke="${color}" stroke-dasharray="${C}" stroke-dashoffset="${offset}"/>
        </svg>
        <span class="ring-pct" style="color:${color}">${Math.round(pct)}%</span>
      </div>
      <span class="ring-label">${esc(w.label)}${reset}</span>
      ${sub}
    </div>`;
}

/* 没有百分比概念的窗口条目：画一个空的中性灰环（不画色弧、不显示百分数），
   真正的文本内容放在环下方的 sub 行——绝不能显示成 100% 满环 */
function renderRingFlat(w) {
  const reset = w.reset_at ? ` · <span class="window-reset">${fmtReset(w.reset_at)}</span>` : "";
  const subText = w.max_label || w.used_label || "";
  const sub = subText ? `<span class="ring-sub">${esc(subText)}</span>` : "";
  return `
    <div class="window-ring ring-flat">
      <div class="ring-wrap">
        <svg viewBox="0 0 64 64">
          <circle class="ring-bg" cx="32" cy="32" r="26" fill="none" stroke-width="6"/>
        </svg>
      </div>
      <span class="ring-label">${esc(w.label)}${reset}</span>
      ${sub}
    </div>`;
}

function renderBar(w) {
  if (noPercentData(w)) return renderBarFlat(w);
  const used = w.used_percent ?? (100 - w.remaining_percent);
  const pct = Math.max(0, Math.min(100, used));
  const remaining = 100 - pct;
  const color = ringColor(remaining);
  const extra = [w.used_label ? `已用 ${w.used_label}` : "", w.max_label ? `限额 ${w.max_label}` : "",
                 w.reset_at ? `重置 ${fmtReset(w.reset_at)}` : ""].filter(Boolean).join(" · ");
  return `
    <div class="window-bar">
      <div class="bar-head">
        <span>${esc(w.label)}</span>
        <span class="bar-pct" style="color:${color}">已用 ${pct.toFixed(0)}%</span>
      </div>
      <div class="bar-track">
        <div class="bar-fill" style="--bar-c:${color};--bar-c2:${color};width:${pct}%"></div>
      </div>
      ${extra ? `<div class="bar-extra">${esc(extra)}</div>` : ""}
    </div>`;
}

/* 没有百分比概念的窗口条目（列表形式）：不画进度条，只展示文本 */
function renderBarFlat(w) {
  const mainText = w.max_label || w.used_label || "";
  const resetText = w.reset_at ? `重置 ${fmtReset(w.reset_at)}` : "";
  return `
    <div class="window-bar bar-flat">
      <div class="bar-head">
        <span>${esc(w.label)}</span>
        ${mainText ? `<span class="bar-flat-value">${esc(mainText)}</span>` : ""}
      </div>
      ${resetText ? `<div class="bar-extra">${esc(resetText)}</div>` : ""}
    </div>`;
}

/* ── 单渠道操作：刷新 / 启停 ────────────────────────────── */

async function onRefreshOneCard(btn) {
  if (btn.disabled) return;
  const id = btn.dataset.id;
  const card = btn.closest(".card");
  btn.disabled = true;
  btn.classList.add("spinning");
  card?.classList.add("card-busy");
  try {
    // 只强刷这一张卡：force=1 强制绕过缓存，ids=id 只让后端查这一个渠道
    // （其余渠道的缓存条目完全不受影响——这正是按 channel id 分别缓存的意义）。
    const data = await fetchQuotas(`/api/quotas?force=1&ids=${encodeURIComponent(id)}`);
    const fresh = data.channels.find((c) => c.id === id);
    if (fresh) {
      // 局部替换这一条数据 + 局部重绘这张卡（不触发全屏重绘，避免其他卡片闪烁）
      const idx = quotas.findIndex((c) => c.id === id);
      if (idx >= 0) quotas[idx] = fresh;
      else quotas.push(fresh);
      renderSummaryChips(quotas);
      const newCard = renderCard(fresh);
      card?.insertAdjacentHTML("afterend", newCard);
      card?.remove();
    }
  } catch (e) {
    toast("刷新该渠道失败: " + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.classList.remove("spinning");
  }
}

async function onToggleEnabled(el) {
  const id = el.dataset.id;
  const type = el.dataset.type;
  const nextEnabled = el.checked;
  el.disabled = true;
  try {
    const res = await fetch("/api/channels", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, type, enabled: nextEnabled }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    toast(nextEnabled ? "渠道已启用" : "渠道已停用", "ok");
    await loadChannels();
    await refreshQuotas(true);
  } catch (err) {
    el.checked = !nextEnabled;
    el.disabled = false;
    toast("操作失败: " + err.message, "err");
  }
}

/* ── 本地用量渲染 ───────────────────────────────────────── */

function renderLocalUsage() {
  const wrap = $("#localUsage");
  if (!localUsage || !Array.isArray(localUsage.sources)) {
    wrap.classList.add("hidden");
    wrap.innerHTML = "";
    return;
  }
  const visible = localUsage.sources.filter((s) => s.available);
  if (!visible.length) {
    wrap.classList.add("hidden");
    wrap.innerHTML = "";
    return;
  }
  wrap.classList.remove("hidden");
  wrap.innerHTML = visible.map((s) => renderLocalSource(s, localUsage.days)).join("");
}

function renderLocalSource(source, days) {
  const t = source.totals || {};
  const fmtTokens = (n) => (n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(n ?? 0));
  const modelStats = source.model_stats || [];
  const hasSessions = modelStats.some((m) => m.sessions !== undefined);
  const countLabel = hasSessions ? "会话" : "assistant 消息";

  const stats = `
    <div class="local-stats">
      <div class="local-stat"><div class="label">近 ${days ?? "-"} 天${countLabel}</div><div class="value">${hasSessions ? t.sessions ?? "-" : t.messages ?? "-"}</div></div>
      <div class="local-stat"><div class="label">输入 tokens</div><div class="value">${fmtTokens(t.input ?? 0)}</div></div>
      <div class="local-stat"><div class="label">输出 tokens</div><div class="value">${fmtTokens(t.output ?? 0)}</div></div>
      <div class="local-stat"><div class="label">缓存读 tokens</div><div class="value">${fmtTokens(t.cache_read ?? 0)}</div></div>
      <div class="local-stat"><div class="label">缓存写 tokens</div><div class="value">${fmtTokens(t.cache_write ?? 0)}</div></div>
      ${t.has_cost ? `<div class="local-stat"><div class="label">估算费用</div><div class="value">$${(t.cost ?? 0).toFixed(2)}</div></div>` : ""}
    </div>`;

  let table = `<div class="local-empty">暂无已完成的会话记录。</div>`;
  if (modelStats.length) {
    const rows = modelStats
      .map((m) => `
        <tr>
          <td class="model">${esc(m.model)}</td>
          <td>${hasSessions ? m.sessions ?? 0 : m.messages ?? 0}</td>
          <td>${fmtTokens(m.input ?? 0)}</td>
          <td>${fmtTokens(m.output ?? 0)}</td>
          ${t.has_cost ? `<td>$${(m.cost ?? 0).toFixed(2)}</td>` : ""}
        </tr>`)
      .join("");
    table = `
      <div class="local-table-wrap">
        <table class="local-table">
          <thead><tr><th>模型</th><th>${countLabel}</th><th>输入</th><th>输出</th>${t.has_cost ? "<th>费用</th>" : ""}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  const note = source.path || source.message || "";
  return `
    <section class="local-section">
      <div class="section-head">
        <h2><span class="section-dot local"></span>${esc(source.label || source.key || "本地统计")}</h2>
        <span class="section-note" title="${esc(note)}">${esc(note)}</span>
      </div>
      <div class="local-body">${stats}${table}</div>
    </section>`;
}

/* ── 配置弹窗 ───────────────────────────────────────────── */

function openConfigModal() {
  $("#configModal").classList.remove("hidden");
  renderTypeSelect();
  resetForm();
  renderChannelsList();
  $("#channelCount").textContent = channels.length;
}

function closeConfigModal() {
  $("#configModal").classList.add("hidden");
}

function renderTypeSelect() {
  const select = $("#fType");
  select.innerHTML = CATEGORY_ORDER
    .map((cat) => {
      const opts = Object.entries(providersCatalog).filter(([, meta]) => meta.category === cat);
      if (!opts.length) return ""; // 该分类下没有任何 provider（如 local），不渲染空 optgroup
      const optsHtml = opts
        .map(([id, meta]) => `<option value="${esc(id)}">${esc(meta.label)}</option>`)
        .join("");
      return `<optgroup label="${esc(categories[cat] || cat)}">${optsHtml}</optgroup>`;
    })
    .join("");
}

function renderDynamicFields(existing = null) {
  const type = $("#fType").value;
  const meta = providersCatalog[type];
  if (!meta) return;
  const container = $("#dynamicFields");
  let fields = "";

  const field = (key, label, placeholderDefault, full = false, typeAttr = "password") => {
    const isSecret = SECRET_FIELDS.includes(key);
    const maskedVal = existing && isSecret ? existing[key] : null;
    const placeholder = maskedVal || placeholderDefault;
    const hint = existing && isSecret
      ? `<div class="field-hint">留空表示不修改${maskedVal ? "" : "（当前未设置）"}</div>`
      : "";
    // 密钥字段加一个显示/隐藏切换按钮（小眼睛），方便用户核对输入的长 API Key
    const toggle = isSecret
      ? `<button type="button" class="pw-toggle" data-toggle="${key}" title="显示/隐藏" aria-label="显示或隐藏 ${esc(label)}" tabindex="-1">
           <svg viewBox="0 0 24 24" class="icon"><path fill="currentColor" d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17a5 5 0 1 1 0-10 5 5 0 0 1 0 10zm0-8a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"/></svg>
         </button>`
      : "";
    return `
      <div class="${full ? "field-full" : ""}${isSecret ? " field-secret" : ""}">
        <label for="f_${key}">${label}</label>
        <div class="input-wrap">
          <input type="${typeAttr}" id="f_${key}" placeholder="${esc(placeholder)}" autocomplete="off">
          ${toggle}
        </div>
        ${hint}
      </div>`;
  };

  // MiMo 渠道的"api_key"实际存的是 Cookie（小米账号 session），不是 sk- key——
  // 表单 label / placeholder 需要相应调整，避免用户误以为是 API Key。
  const isMimo = type === "mimo";

  for (const f of meta.fields) {
    if (f === "api_key") {
      if (isMimo) {
        fields += field("api_key", "Cookie", "登录 platform.xiaomimimo.com 后从浏览器复制完整 Cookie", true, "text");
      } else {
        fields += field("api_key", "API Key", "sk-...");
      }
    }
    else if (f === "base_url") fields += field("base_url", "Base URL", "https://...", true, "text");
    else if (f === "ak") fields += field("ak", "AccessKey ID", "火山控制台获取", false, "text");
    else if (f === "sk") fields += field("sk", "Secret AccessKey", "火山控制台获取");
    else if (f === "region") fields += field("region", "Region（默认 cn-beijing）", "cn-beijing", false, "text");
    else if (f === "organization") fields += field("organization", "组织 ID（可选）", "", false, "text");
    else if (f === "project") fields += field("project", "项目 ID（可选）", "", false, "text");
  }

  const hint = isMimo
    ? "MiMo 用量查询需要小米账号登录后的 Cookie（不是 API Key）。请登录 platform.xiaomimimo.com，从浏览器开发者工具复制完整 Cookie 填入。Cookie 只保存在本地 config.json（权限 600），仅用于只读查询。"
    : meta.category === "subscription"
      ? "订阅类渠道无需填写密钥：自动读取本机 CLI 的登录凭据（只读，不刷新不写入）。"
      : meta.category === "local"
        ? "本地统计无需任何密钥，直接读取本地数据库。"
        : "密钥只保存在本地 config.json（权限 600），仅用于查询余额。";

  container.innerHTML = fields + `<div class="form-hint">${hint}</div>`;
}

function resetForm() {
  editingId = null;
  $("#channelForm").reset();
  $("#fEnabled").checked = true;
  renderTypeSelect();
  $("#fType").value = "deepseek";
  renderDynamicFields();
  $("#btnFormSave").textContent = "保存渠道";
  $("#btnFormReset").classList.add("hidden");
}

function fillForm(ch) {
  editingId = ch.id;
  $("#fType").value = ch.type;
  renderDynamicFields(ch);
  $("#fName").value = ch.name || "";
  $("#fEnabled").checked = ch.enabled !== false;
  const set = (key, val) => { const el = $(`#f_${key}`); if (el) el.value = val || ""; };
  // 密钥字段（api_key / ak / sk）故意不回填：占位符里已经显示打码后的旧值，
  // 留空提交表示"沿用原值"。回填打码串会导致保存时把真实密钥覆盖成打码串。
  set("base_url", ch.base_url || "");
  set("region", ch.region || "");
  set("organization", ch.organization || "");
  set("project", ch.project || "");
  $("#btnFormSave").textContent = "更新渠道";
  $("#btnFormReset").classList.remove("hidden");
  // modal 本身是覆盖层（可滚动），滚 window 没有意义；把表单滚动到可见区域顶部
  $("#channelForm").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function onSaveChannel(e) {
  e.preventDefault();
  const type = $("#fType").value;
  const meta = providersCatalog[type];
  const payload = {
    id: editingId || undefined,
    type,
    name: $("#fName").value.trim() || meta.default_name,
    enabled: $("#fEnabled").checked,
  };
  const read = (key) => { const el = $(`#f_${key}`); return el ? el.value.trim() : ""; };
  for (const f of meta.fields) {
    payload[f] = read(f);
  }

  const fieldLabel = (r) => (r === "api_key" ? "API Key" : r === "ak" ? "AccessKey ID" : r === "sk" ? "Secret" : "Base URL");

  // base_url 始终必填；密钥类字段（api_key/ak/sk）只在"新建"时必填。
  // 编辑时留空表示沿用原值——后端会保留旧密钥，不会被清空，所以不能强制要求重新输入。
  for (const r of ["base_url"]) {
    if (meta.fields.includes(r) && !payload[r]) {
      toast(`请填写 ${fieldLabel(r)}`, "err");
      return;
    }
  }
  if (!editingId) {
    for (const r of SECRET_FIELDS) {
      if (meta.fields.includes(r) && !payload[r]) {
        toast(`请填写 ${fieldLabel(r)}`, "err");
        return;
      }
    }
  } else {
    // 编辑时：密钥字段留空就不提交该字段，更明确地表达"未修改"，避免传空字符串产生歧义
    for (const s of SECRET_FIELDS) {
      if (meta.fields.includes(s) && !payload[s]) {
        delete payload[s];
      }
    }
  }

  try {
    const res = await fetch("/api/channels", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    toast(editingId ? "渠道已更新" : "渠道已添加", "ok");
    resetForm();
    await loadChannels();
    await refreshQuotas(true);
  } catch (err) {
    toast("保存失败: " + err.message, "err");
  }
}

function renderChannelsList() {
  const container = $("#channelsList");
  if (!channels.length) {
    container.innerHTML = `<div class="local-empty">尚未配置渠道，从上方表单添加。</div>`;
    return;
  }
  container.innerHTML = channels
    .map((ch) => {
      const meta = PROVIDER_META[ch.type] || FALLBACK_META;
      const key = ch.api_key || ch.ak || "自动读取";
      return `
        <div class="channel-row">
          <div class="icon-tile" style="--tile-a:${meta.a};--tile-b:${meta.b};width:30px;height:30px;font-size:12px">${esc(meta.icon)}</div>
          <div class="row-name">
            <div class="t">${esc(ch.name)} ${ch.enabled ? "" : '<span class="status-tag disabled">停用</span>'}</div>
            <div class="ty">${esc(providersCatalog[ch.type]?.label || ch.type)}</div>
          </div>
          <span class="row-key">${esc(key)}</span>
          <div class="row-actions">
            <button type="button" class="btn-icon" title="编辑" data-action="edit-channel" data-id="${esc(ch.id)}">
              <svg viewBox="0 0 24 24" class="icon" style="width:15px;height:15px"><path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
            </button>
            <button type="button" class="btn-icon" title="删除" data-action="delete-channel" data-id="${esc(ch.id)}" style="color:var(--red)">
              <svg viewBox="0 0 24 24" class="icon" style="width:15px;height:15px"><path fill="currentColor" d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
            </button>
          </div>
        </div>`;
    })
    .join("");
}

async function deleteChannel(id) {
  const ch = channels.find((c) => c.id === id);
  if (!confirm(`确定删除渠道「${ch?.name || id}」？`)) return;
  try {
    const res = await fetch(`/api/channels/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    toast("渠道已删除", "ok");
    await loadChannels();
    await refreshQuotas(true);
  } catch (e) {
    toast("删除失败: " + e.message, "err");
  }
}

/* ── 工具 ───────────────────────────────────────────────── */

function fmtTime(ms) {
  const d = new Date(ms);
  const now = Date.now();
  const diff = now - ms;
  if (diff < 60_000) return "刚刚";
  if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  return d.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
}

function fmtReset(ms) {
  const d = new Date(ms);
  const now = Date.now();
  const diff = ms - now;
  if (diff <= 0) return "已重置";
  if (diff < 3600_000) return `${Math.ceil(diff / 60_000)} 分钟后`;
  if (diff < 86400_000) return `${Math.ceil(diff / 3600_000)} 小时后`;
  return `${Math.ceil(diff / 86400_000)} 天后`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function toast(message, kind = "info") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity 0.3s, transform 0.3s";
    el.style.opacity = "0";
    el.style.transform = "translateX(18px)";
    setTimeout(() => el.remove(), 320);
  }, 3500);
}

/* ── 主题三选 ─────────────────────────────────────────── */

function applyStoredTheme() {
  const theme = loadPrefs().theme || "auto";
  document.documentElement.dataset.theme = theme;
}

/* ── 设置弹窗 ─────────────────────────────────────────── */

function openSettingsModal() {
  $("#settingsModal").classList.remove("hidden");
  renderThemeOptions();
  renderRefreshOptions();
  renderThresholdList();
}

function closeSettingsModal() {
  $("#settingsModal").classList.add("hidden");
}

function renderThemeOptions() {
  const current = loadPrefs().theme || "auto";
  $("#themeOptions").innerHTML = THEME_OPTIONS.map(
    (o) => `<button type="button" class="seg-btn${o.value === current ? " active" : ""}" data-theme-val="${o.value}">${o.label}</button>`
  ).join("");
  $("#themeOptions").querySelectorAll("[data-theme-val]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const val = btn.dataset.themeVal;
      savePrefs({ theme: val });
      applyStoredTheme();
      renderThemeOptions();
      // 主题切换后重绘以让取色生效
      renderDashboard();
      renderLocalUsage();
    });
  });
}

function renderRefreshOptions() {
  const current = getRefreshInterval();
  const autoOn = $("#autoRefresh").checked;
  $("#refreshOptions").innerHTML =
    REFRESH_INTERVALS.map(
      (o) => `<button type="button" class="seg-btn${o.value === current ? " active" : ""}${!autoOn && o.value > 0 ? " dim" : ""}" data-refresh-val="${o.value}">${o.label}</button>`
    ).join("");
  $("#refreshOptions").querySelectorAll("[data-refresh-val]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const val = Number(btn.dataset.refreshVal);
      savePrefs({ refreshInterval: val });
      if (val === 0) {
        $("#autoRefresh").checked = false;
        clearInterval(autoRefreshTimer);
      } else {
        $("#autoRefresh").checked = true;
        setupAutoRefresh();
      }
      renderRefreshOptions();
    });
  });
}

/* ── 低余额阈值告警 ─────────────────────────────────────── */

const DEFAULT_THRESHOLD = 20; // 默认剩余百分比阈值

function getThresholds() {
  return loadPrefs().thresholds || {};
}

function setThreshold(channelId, value) {
  const thresholds = getThresholds();
  if (value === null || value === "" || isNaN(value)) {
    delete thresholds[channelId];
  } else {
    thresholds[channelId] = Math.max(0, Math.min(100, Number(value)));
  }
  savePrefs({ thresholds });
  renderSummaryChips(quotas); // 阈值变化立即更新汇总徽章
}

function renderThresholdList() {
  const container = $("#thresholdList");
  if (!channels.length) {
    container.innerHTML = `<div class="local-empty">尚未配置渠道。先添加渠道后再设置阈值。</div>`;
    return;
  }
  const thresholds = getThresholds();
  container.innerHTML = channels
    .map((ch) => {
      const meta = PROVIDER_META[ch.type] || FALLBACK_META;
      const val = thresholds[ch.id] ?? "";
      return `
        <div class="threshold-row">
          <div class="icon-tile" style="--tile-a:${meta.a};--tile-b:${meta.b};width:28px;height:28px;font-size:11px">${esc(meta.icon)}</div>
          <div class="threshold-name">
            <div class="t">${esc(ch.name)}</div>
            <div class="ty">${esc(providersCatalog[ch.type]?.label || ch.type)}</div>
          </div>
          <div class="threshold-input-wrap">
            <span class="threshold-suffix">剩余 &lt;</span>
            <input type="number" min="0" max="100" placeholder="${DEFAULT_THRESHOLD}" value="${val}" data-threshold-id="${esc(ch.id)}">
            <span class="threshold-suffix">% 告警</span>
          </div>
        </div>`;
    })
    .join("");
  container.querySelectorAll("[data-threshold-id]").forEach((input) => {
    input.addEventListener("change", () => {
      setThreshold(input.dataset.thresholdId, input.value);
    });
  });
}

/* 判断某渠道是否触发阈值告警（remaining_percent < 阈值）。
   只对有百分比窗口的 ok 渠道生效；info/error/disabled 不参与。 */
function channelBreachesThreshold(q) {
  if (q.status !== "ok" || !q.windows) return false;
  const threshold = getThresholds()[q.id];
  if (threshold === undefined) return false;
  // 任一百分比窗口剩余低于阈值即告警
  return q.windows.some(
    (w) => w.remaining_percent !== null && w.remaining_percent !== undefined && w.remaining_percent < threshold
  );
}

/* ── 配置导入/导出 ─────────────────────────────────────── */

function _downloadJSON(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function exportConfig(includeSecrets) {
  try {
    const res = await fetch(`/api/config/export?include_secrets=${includeSecrets ? "true" : "false"}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const tag = includeSecrets ? "full" : "safe";
    const date = new Date().toISOString().slice(0, 10);
    _downloadJSON(data, `quotaboard-${tag}-${date}.json`);
    toast(includeSecrets ? "已导出含密钥的完整配置（请妥善保管）" : "已导出脱敏配置（不含密钥，可分享）", "ok");
  } catch (e) {
    toast("导出失败: " + e.message, "err");
  }
}

async function onImportFileSelected(e) {
  const file = e.target.files[0];
  e.target.value = ""; // 允许重复选同一文件
  if (!file) return;
  let data;
  try {
    data = JSON.parse(await file.text());
  } catch (err) {
    toast("文件不是合法的 JSON: " + err.message, "err");
    return;
  }
  const mode = confirm(
    "选择导入方式：\n\n" +
      "• 确定 = 合并导入（追加到现有配置，同 id 覆盖）\n" +
      "• 取消 = 替换导入（清空现有全部渠道后替换）\n\n" +
      "合并模式更安全，推荐。"
  )
    ? "merge"
    : "replace";
  if (mode === "replace") {
    if (!confirm("替换模式会清空当前全部渠道，确定继续？")) return;
  }
  try {
    const res = await fetch("/api/config/import?mode=" + mode, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const result = await res.json();
    toast(`导入成功（${mode === "merge" ? "合并" : "替换"}），当前共 ${result.count} 个渠道`, "ok");
    await loadChannels();
    await refreshQuotas(true);
    closeConfigModal();
  } catch (err) {
    toast("导入失败: " + err.message, "err");
  }
}

/* ── 历史趋势 ─────────────────────────────────────────── */

let historyCache = null;

function openHistoryModal() {
  $("#historyModal").classList.remove("hidden");
  // 渠道下拉用当前已配置的渠道填充
  const sel = $("#historyChannel");
  const current = sel.value;
  sel.innerHTML = `<option value="">全部渠道</option>` +
    channels
      .map((ch) => `<option value="${esc(ch.id)}">${esc(ch.name)}</option>`)
      .join("");
  if (current) sel.value = current;
  loadHistory();
}

function closeHistoryModal() {
  $("#historyModal").classList.add("hidden");
}

async function loadHistory() {
  const days = $("#historyDays").value;
  const cid = $("#historyChannel").value;
  const container = $("#historyContent");
  container.innerHTML = `<div class="history-loading">加载中…</div>`;
  try {
    const url = `/api/history?days=${days}${cid ? `&ids=${encodeURIComponent(cid)}` : ""}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    historyCache = await res.json();
    renderHistory(historyCache, Number(days));
  } catch (e) {
    container.innerHTML = `<div class="empty-state error-state"><div class="empty-icon">⚠️</div><p>加载趋势失败: ${esc(e.message)}</p></div>`;
  }
}

function renderHistory(data, days) {
  const container = $("#historyContent");
  const entries = Object.entries(data.channels || {});
  if (!entries.length || entries.every(([, recs]) => !recs.length)) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📈</div>
        <h3>暂无趋势数据</h3>
        <p>每次成功的额度查询会自动记录一条趋势点。继续使用一段时间后这里会出现折线图。</p>
      </div>`;
    return;
  }

  container.innerHTML = entries
    .map(([cid, records]) => renderHistoryChart(cid, records, days))
    .join("");
}

function renderHistoryChart(channelId, records, days) {
  const ch = channels.find((c) => c.id === channelId);
  const name = ch?.name || channelId;
  const meta = PROVIDER_META[ch?.type] || FALLBACK_META;

  if (!records.length) {
    return `<div class="history-card empty"><div class="history-head"><span class="icon-tile sm" style="--tile-a:${meta.a};--tile-b:${meta.b}">${esc(meta.icon)}</span><span>${esc(name)}</span></div><div class="history-empty">该渠道在所选范围内无记录。</div></div>`;
  }

  // 提取所有窗口 key，每个 key 画一条折线（剩余百分比）
  const windowKeys = new Map(); // key -> label
  for (const r of records) {
    for (const w of r.windows || []) {
      if (w.key && !windowKeys.has(w.key)) windowKeys.set(w.key, w.label || w.key);
    }
  }
  // 余额类渠道画 amount.value
  const hasAmount = records.some((r) => r.amount && typeof r.amount.value === "number");

  const series = [];
  if (hasAmount) {
    series.push({
      label: "余额",
      points: records
        .filter((r) => r.amount && typeof r.amount.value === "number")
        .map((r) => [r.ts, r.amount.value]),
      color: meta.a,
      kind: "amount",
    });
  }
  for (const [key, label] of windowKeys) {
    const pts = records
      .map((r) => {
        const w = (r.windows || []).find((x) => x.key === key);
        if (!w || w.remaining_percent === null || w.remaining_percent === undefined) return null;
        return [r.ts, w.remaining_percent];
      })
      .filter(Boolean);
    if (pts.length) series.push({ label, points: pts, color: seriesColor(series.length), kind: "percent" });
  }

  if (!series.length) {
    return `<div class="history-card"><div class="history-head"><span class="icon-tile sm" style="--tile-a:${meta.a};--tile-b:${meta.b}">${esc(meta.icon)}</span><span>${esc(name)}</span></div><div class="history-empty">该渠道的趋势记录无可绘制的数值字段。</div></div>`;
  }

  return `<div class="history-card">
    <div class="history-head">
      <span class="icon-tile sm" style="--tile-a:${meta.a};--tile-b:${meta.b}">${esc(meta.icon)}</span>
      <span class="history-title">${esc(name)}</span>
    </div>
    ${series.map((s) => renderSparkline(s, records)).join("")}
  </div>`;
}

function seriesColor(idx) {
  const palette = ["#34d399", "#fbbf24", "#60a5fa", "#f472b6", "#a78bfa", "#22d3ee"];
  return palette[idx % palette.length];
}

/* 用 SVG 画一条简洁的折线（sparkline），无第三方依赖 */
function renderSparkline(series, allRecords) {
  const W = 520, H = 120, pad = 28;
  const pts = series.points;
  if (!pts.length) return ""; // 防御：空序列不画
  const tsValues = allRecords.map((r) => r.ts);
  const tsMin = Math.min(...tsValues);
  const tsMax = Math.max(...tsValues);
  // 单点或时间戳相同（同一天多次记录被去重后只剩一条）时 tsSpan=0 会导致除零，
  // 这里给它一个最小跨度，让唯一的点画在中间附近而不是溢出 Infinity。
  const tsSpan = Math.max(1, tsMax - tsMin);
  const valMin = series.kind === "percent" ? 0 : Math.min(...pts.map((p) => p[1]));
  const valMax = series.kind === "percent" ? 100 : Math.max(...pts.map((p) => p[1]));
  // valMax === valMin（所有值相同，如恒定余额）时 valSpan=0 会让 y() 溢出，
  // 用一个小跨度兜底；percent 类型固定 0-100 不受影响。
  const valSpan = series.kind === "percent" ? 100 : Math.max(1e-9, valMax - valMin);

  const x = (ts) => pad + ((ts - tsMin) / tsSpan) * (W - pad * 2);
  const y = (val) => H - pad - ((val - valMin) / valSpan) * (H - pad * 2);

  const pathD = pts.map((p, i) => `${i === 0 ? "M" : "L"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
  const dots = pts.map((p) => `<circle cx="${x(p[0]).toFixed(1)}" cy="${y(p[1]).toFixed(1)}" r="2.5" fill="${series.color}"/>`).join("");
  const firstVal = pts[0][1];
  const lastVal = pts[pts.length - 1][1];
  const unit = series.kind === "percent" ? "%" : "";
  const delta = lastVal - firstVal;
  const deltaTxt = delta >= 0 ? `+${delta.toFixed(1)}${unit}` : `${delta.toFixed(1)}${unit}`;

  return `
    <div class="sparkline-wrap">
      <div class="sparkline-head">
        <span class="sparkline-label" style="--sl-c:${series.color}">${esc(series.label)}</span>
        <span class="sparkline-stats"><b>${lastVal.toFixed(1)}${unit}</b> <span class="sparkline-delta ${delta >= 0 ? "up" : "down"}">${deltaTxt}</span></span>
      </div>
      <svg class="sparkline" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        ${series.kind === "percent" ? `<line x1="${pad}" y1="${y(0)}" x2="${W - pad}" y2="${y(0)}" class="sl-baseline"/>` : ""}
        <path d="${pathD}" fill="none" stroke="${series.color}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>
        ${dots}
      </svg>
    </div>`;
}

init();
