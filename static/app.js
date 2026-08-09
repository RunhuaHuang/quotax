/* ── QuotaX 前端逻辑 ─────────────────────────────────── */

// 不依赖 DOM/localStorage 的纯函数抽到 view-utils.js（可被 node:test 直接
// import 测试），这里只 import 使用，避免同一份逻辑两处维护。
import { normalizeThreeWindows, canonicalChannelId, channelBreachesThreshold, noPercentData, fmtReset } from "./view-utils.js?v=7";
import { t, thas, toggleLang, getLang } from "./i18n.js?v=7";

const $ = (sel) => document.querySelector(sel);

/* 翻译 HTML 中的静态节点（data-i18n / data-i18n-title / data-i18n-placeholder / data-i18n-aria） */
function applyStaticI18n(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  root.querySelectorAll("[data-i18n-title]").forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
  });
  root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
  root.querySelectorAll("[data-i18n-aria]").forEach((el) => {
    el.setAttribute("aria-label", t(el.dataset.i18nAria));
  });
}

/* 取 provider / category 的翻译标签，兜底后端返回的原始值。
   必须用 thas() 判断翻译是否存在——t() 在 key 缺失时返回 key 本身（非空），
   直接 `t() || fallback` 的 fallback 永远不会触发。 */
function providerLabel(type) {
  const key = `provider.${type}`;
  return thas(key) ? t(key) : (providersCatalog[type]?.label || type);
}
function providerDefaultName(type) {
  const key = `providerDefault.${type}`;
  return thas(key) ? t(key) : (providersCatalog[type]?.default_name || type);
}
function categoryLabel(cat) {
  const key = `category.${cat}`;
  return thas(key) ? t(key) : (categories[cat] || cat);
}

const CATEGORY_ORDER = ["coding_plan", "subscription", "balance", "local"];

/* 编辑渠道时需要"留空表示不修改"的密钥字段 */
const SECRET_FIELDS = ["api_key", "ak", "sk"];

/* 本地用量统计默认拉取的天数（展示文案用后端返回的真实 days，这里只是请求参数） */
const LOCAL_USAGE_DAYS = 14;

/* 各渠道的视觉元数据 */
const PROVIDER_META = {
  deepseek:            { icon: "D", logo: "/static/icons/deepseek.png",  a: "#4d6bfe", b: "#2b3fb3", cat: "balance" },
  stepfun:             { icon: "阶", logo: "/static/icons/stepfun.png",  a: "#7c3aed", b: "#4c1d95", cat: "balance" },
  siliconflow:         { icon: "硅", logo: "/static/icons/siliconflow.png", a: "#0ea5e9", b: "#075985", cat: "balance" },
  openrouter:          { icon: "OR", logo: "/static/icons/openrouter.svg",  a: "#f59e0b", b: "#b45309", cat: "balance" },
  novita:              { icon: "N", logo: "/static/icons/novita.png",       a: "#ec4899", b: "#9d174d", cat: "balance" },
  kimi_api:            { icon: "K", logo: "/static/icons/kimi.png",      a: "#3b82f6", b: "#1e40af", cat: "balance" },
  newapi:              { icon: "中", logo: "/static/icons/newapi.png",      a: "#10b981", b: "#065f46", cat: "balance" },
  kimi_coding:         { icon: "K", logo: "/static/icons/kimi.png",      a: "#3b82f6", b: "#1e40af", cat: "coding_plan" },
  zhipu_coding:        { icon: "智", logo: "/static/icons/zhipu.png",     a: "#6366f1", b: "#3730a3", cat: "coding_plan" },
  zhipu_team:          { icon: "团", logo: "/static/icons/zhipu.png",     a: "#8b5cf6", b: "#5b21b6", cat: "coding_plan" },
  minimax:             { icon: "M", logo: "/static/icons/minimax.png",   a: "#22d3ee", b: "#0e7490", cat: "coding_plan" },
  volcengine:          { icon: "火", logo: "/static/icons/volcengine.png", a: "#f43f5e", b: "#9f1239", cat: "coding_plan" },
  zenmux:              { icon: "Z", logo: "/static/icons/zenmux.png",      a: "#a3e635", b: "#4d7c0f", cat: "coding_plan" },
  mimo:                { icon: "米", logo: "/static/icons/mimo.png",      a: "#ff6900", b: "#c2410c", cat: "coding_plan" },
  claude_subscription: { icon: "Cl", logo: "/static/icons/claude.png",   a: "#d97757", b: "#9a3412", cat: "subscription" },
  gemini_subscription: { icon: "G", logo: "/static/icons/gemini.png",    a: "#4285f4", b: "#1a3a8f", cat: "subscription" },
  grok_subscription:   { icon: "G", logo: "/static/icons/grok.png",      a: "#94a3b8", b: "#475569", cat: "subscription" },
  codex_subscription:  { icon: "C", logo: "/static/icons/codex.png",     a: "#10a37f", b: "#0b5c48", cat: "subscription" },
  copilot_subscription:{ icon: "GH", logo: "/static/icons/copilot.svg",   a: "#8b949e", b: "#30363d", cat: "subscription" },
  opencode_subscription:{ icon: "OC", logo: "/static/icons/opencode.svg", a: "#a855f7", b: "#6b21a8", cat: "subscription" },
};

const FALLBACK_META = { icon: "?", a: "var(--accent)", b: "var(--accent-2)", cat: "balance" };

/* 统一的图标渲染：有 logo 图片时显示 <img>（图片自适应缩放），否则回退到文字字母。
   cls 为额外 class（如 "sm" 小尺寸），styleExtra 为额外内联样式（如阈值列表的 28px）。 */
function iconTile(meta, cls = "", styleExtra = "") {
  const clsStr = cls ? ` ${cls}` : "";
  const style = `--tile-a:${meta.a};--tile-b:${meta.b};${styleExtra}`;
  if (meta.logo) {
    return `<div class="icon-tile icon-tile-logo${clsStr}" style="${style}"><img src="${esc(meta.logo)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{textContent:'${esc(meta.icon)}'}))"></div>`;
  }
  return `<div class="icon-tile${clsStr}" style="${style}">${esc(meta.icon)}</div>`;
}

function statusText(status) {
  const key = `status.${status}`;
  return thas(key) ? t(key) : status;
}
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

/* 自动刷新间隔（毫秒）。默认 5 分钟——自动刷新是"兜底保活"，用户想立刻看
   最新数据时点顶栏「刷新」按钮即可强制全刷（force=1 绕过后端缓存，真查所有
   渠道）。后端成功缓存 5 分钟 / 失败缓存 15 分钟（避免频繁请求触发上游 429 限流）。
   间隔大于等于缓存 TTL 时定时刷新才能拿到新数据；间隔小于 TTL 时刷新会命中缓存
   （节省上游请求），点「刷新」按钮可强制绕过。用户可在设置里调整频率。 */
const REFRESH_INTERVALS = [
  { value: 0, labelKey: "refresh.off" },
  { value: 30_000, labelKey: "refresh.30s" },
  { value: 60_000, labelKey: "refresh.1m" },
  { value: 90_000, labelKey: "refresh.90s" },
  { value: 180_000, labelKey: "refresh.3m" },
  { value: 300_000, labelKey: "refresh.5m" },
  { value: 900_000, labelKey: "refresh.15m" },
  { value: 1_800_000, labelKey: "refresh.30m" },
  { value: 3_600_000, labelKey: "refresh.1h" },
  { value: 10_800_000, labelKey: "refresh.3h" },
];
const DEFAULT_REFRESH_INTERVAL = 300_000;

/* 主题：auto = 跟随系统；light / dark 手动覆盖 */
const THEME_OPTIONS = [
  { value: "auto", labelKey: "settings.themeAuto" },
  { value: "light", labelKey: "settings.themeLight" },
  { value: "dark", labelKey: "settings.themeDark" },
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
  bindEvents();
  applyStoredTheme(); // 尽早应用主题，避免首屏闪烁
  updateAutoRefreshLabel(); // 首屏就显示「自动刷新 · 5 分钟」（在 applyStaticI18n 之前，
                            // 因为该 label 不带 data-i18n，不会被 i18n 覆盖）
  applyStaticI18n();  // 翻译 HTML 中的静态节点
  // 会话快照：整页刷新/重新打开时**同步**恢复上次画面（脚本一执行就渲染，
  // 不闪骨架屏、不等网络），新数据在后台到达后无缝替换。只有完全没有快照的
  // 首次打开才显示骨架屏。
  const restored = restoreSnapshot();
  if (!restored) renderSkeletonState();
  try {
    await loadProviders();
  } catch (e) {
    console.error(e);
    renderFatalError(t("empty.initFailedTitle") + `: ${t("empty.quotaLoadFailedDesc")} (${e.message})`);
    return;
  }
  // 快照恢复时 providersCatalog 可能还没就绪（类型标签用 fallback），这里重绘修正
  if (dashboardLoadedOnce) renderDashboard();
  await Promise.all([loadChannels(), refreshQuotas(true), loadLocalUsage()]);
  setupAutoRefresh();
  updateAutoRefreshLabel();
  setupVisibilityAutoRefresh();
  setupThemeReactivity();
  setupLangReactivity();
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
  // 编辑已有渠道时输入框为空（密钥故意不回填），首次点击眼睛会从后端拉明文密钥填入。
  $("#dynamicFields").addEventListener("click", async (e) => {
    const btn = e.target.closest(".pw-toggle");
    if (!btn) return;
    const input = btn.parentElement.querySelector("input");
    if (!input) return;
    const isPw = input.type === "password";
    // 切换为显示，且当前输入框为空（编辑模式下密钥未回填）→ 从后端拉明文
    if (isPw && !input.value && editingId) {
      btn.style.opacity = "0.5";
      try {
        const res = await fetch(`/api/channels/${encodeURIComponent(editingId)}/secret`);
        if (res.ok) {
          const data = await res.json();
          const key = btn.dataset.toggle;
          const realVal = data.secret?.[key];
          if (realVal) input.value = realVal;
        }
      } catch {
        /* 拉取失败不影响切换，只是看不到明文 */
      } finally {
        btn.style.opacity = "";
      }
    }
    input.type = isPw ? "text" : "password";
    btn.classList.toggle("active", isPw);
  });
  $("#autoRefresh").addEventListener("change", (e) => {
    if (e.target.checked) setupAutoRefresh();
    else clearInterval(autoRefreshTimer);
    updateAutoRefreshLabel();
  });

  // 语言切换
  $("#btnLang").addEventListener("click", () => {
    toggleLang();
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
  $("#btnAutoDetect").addEventListener("click", onAutoDetect);

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
    else if (action === "edit-card") onEditCard(btn.dataset.id);
  });
  $("#dashboard").addEventListener("change", (e) => {
    const el = e.target.closest('[data-action="toggle-enabled"]');
    if (el) onToggleEnabled(el);
  });

  // 渠道卡片拖拽排序：拖把手（card-drag-handle）移动整卡，drop 交换并持久化。
  // 不监听 dragleave——dragover 每帧重建高亮更稳。
  $("#dashboard").addEventListener("dragstart", (e) => {
    const handle = e.target.closest(".card-drag-handle");
    if (!handle) return;
    const card = handle.closest(".card");
    if (!card) return;
    card.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", "card");
  });
  $("#dashboard").addEventListener("dragend", (e) => {
    document.querySelectorAll(".card").forEach((c) => c.classList.remove("dragging", "drop-target"));
  });
  $("#dashboard").addEventListener("dragover", (e) => {
    const card = e.target.closest(".card");
    if (!card || card.classList.contains("dragging")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    document.querySelectorAll(".card.drop-target").forEach((c) => c.classList.remove("drop-target"));
    card.classList.add("drop-target");
  });
  $("#dashboard").addEventListener("drop", (e) => {
    const card = e.target.closest(".card");
    if (!card || card.classList.contains("dragging")) return;
    e.preventDefault();
    onDropCard(card);
    document.querySelectorAll(".card").forEach((c) => c.classList.remove("drop-target"));
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

  // 视图 Tab 切换：额度 / 用量统计。切换时隐藏 / 显示对应 pane，
  // 并通过自定义事件通知 usage.js 首次进入用量视图时加载数据。
  $("#viewTabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".view-tab");
    if (!tab) return;
    switchView(tab.dataset.view);
  });
}

function switchView(view) {
  const tabs = document.querySelectorAll(".view-tab");
  const panes = document.querySelectorAll(".view-pane");
  tabs.forEach((t) => {
    const on = t.dataset.view === view;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  });
  panes.forEach((p) => p.classList.toggle("active", p.id === `view-${view}`));
  // 通知用量统计模块：切到它的视图了（它自己决定是否要首次加载）
  document.dispatchEvent(new CustomEvent("quotax:view-change", { detail: { view } }));
}
window.switchView = switchView;

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
    renderChannelCount();
  } catch (e) {
    toast(t("toast.loadChannelsFailed", { msg: e.message }), "err");
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
    const ts = now.toLocaleTimeString(getLang() === "zh-CN" ? "zh-CN" : "en-US", { hour12: false });
    $("#lastUpdated").textContent = t("card.lastUpdated", { time: ts, cached: data.cached ? t("card.cachedSuffix") : "" });
    saveSnapshot({ channels: data.channels, lastUpdated: ts });
  } catch (e) {
    toast(t("toast.refreshFailed", { ctx: dashboardLoadedOnce ? t("toast.refreshCtxKept") : "", msg: e.message }), "err");
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
    saveSnapshot({ localUsage });
  } catch (e) {
    localUsage = null;
    $("#localUsage").classList.add("hidden");
    $("#localUsage").innerHTML = "";
    toast(t("toast.localUsageFailed", { msg: e.message }), "err");
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

/* 在自动刷新开关的 label 上显示当前刷新频率（如「自动刷新 · 5m」），
   这样用户不用打开设置就能看到间隔。刷新频率变化或语言切换后都要调用。 */
function updateAutoRefreshLabel() {
  const interval = getRefreshInterval();
  const label = $("#autoRefresh").parentElement.querySelector(".switch-label");
  if (!label) return;
  if (interval === 0) {
    label.textContent = t("app.autoRefresh");
  } else {
    const opt = REFRESH_INTERVALS.find((o) => o.value === interval);
    const short = opt ? t(opt.labelKey) : `${Math.round(interval / 60_000)}m`;
    label.textContent = `${t("app.autoRefresh")} · ${short}`;
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
    // auto 模式下系统主题变化要同步更新 <html> dark class，否则光重绘取色不对
    if ((loadPrefs().theme || "light") === "auto") applyStoredTheme();
    renderDashboard();
    renderLocalUsage();
  });
}

/* ── 仪表盘渲染 ─────────────────────────────────────────── */

const SNAPSHOT_KEY = "quotaboard_snapshot";

function saveSnapshot(patch) {
  try {
    const cur = JSON.parse(sessionStorage.getItem(SNAPSHOT_KEY) || "{}");
    Object.assign(cur, patch);
    sessionStorage.setItem(SNAPSHOT_KEY, JSON.stringify(cur));
  } catch {}
}

function restoreSnapshot() {
  try {
    const snap = JSON.parse(sessionStorage.getItem(SNAPSHOT_KEY) || "null");
    if (!snap || typeof snap !== "object") return false;
    let restored = false;
    if (Array.isArray(snap.channels) && snap.channels.length) {
      quotas = snap.channels;
      dashboardLoadedOnce = true;
      renderDashboard();
      if (snap.lastUpdated) {
        $("#lastUpdated").textContent = t("card.lastUpdated", { time: snap.lastUpdated, cached: t("card.restoringSnapshot") });
      }
      restored = true;
    }
    if (snap.localUsage) {
      localUsage = snap.localUsage;
      renderLocalUsage();
      restored = true;
    }
    return restored;
  } catch {
    return false;
  }
}

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
      <h3>${t("empty.initFailedTitle")}</h3>
      <p>${esc(message)}</p>
      <button type="button" class="btn btn-primary" data-action="retry-init">${t("empty.retryReload")}</button>
    </div>`;
}

function renderFatalDashboardError(message) {
  $("#dashboard").innerHTML = `
    <div class="empty-state error-state">
      <div class="empty-icon">⚠️</div>
      <h3>${t("empty.quotaLoadFailedTitle")}</h3>
      <p>${esc(message || t("empty.quotaLoadFailedDesc"))}</p>
      <button type="button" class="btn btn-primary" data-action="retry-refresh">${t("empty.retryRefresh")}</button>
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
        <h3>${t("empty.noChannelsTitle")}</h3>
        <p>${t("empty.noChannelsDesc")}</p>
        <button type="button" class="btn btn-primary" data-action="open-config">${t("empty.addFirstChannel")}</button>
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
  // 卡片顺序：以用户拖拽顺序（localStorage card_order）为主——拖到哪就停在哪，
  // 不再用名字字母序覆盖用户的显式排序（之前的 bug：先按名字 localeCompare，
  // 名字不同就直接 return，_cardRank 只在同名时才比较，于是不同名渠道拖了等于没拖）。
  // card_order 里没记录的渠道（新建的 / 第一次出现的）追加在已排序渠道之后，
  // 之间用名字字母序做稳定 tiebreaker，保证每次刷新顺序不抖动。
  const cardOrder = loadCardOrder();

  container.innerHTML = [...knownCats, ...extraCats]
    .map((cat) => {
      const items = [...groups[cat]]
        .sort((a, b) => {
          const ra = _cardRank(cardOrder, a.id);
          const rb = _cardRank(cardOrder, b.id);
          if (ra !== rb) return ra - rb;
          return (a.name || "").localeCompare(b.name || "", "zh");
        })
        .map(renderCard)
        .join("");
      return `
        <section class="section">
          <div class="section-head">
            <h2><span class="section-dot ${esc(cat)}"></span>${esc(categoryLabel(cat))}</h2>
            <span class="section-note">${t("section.channelsCount", { count: groups[cat].length })}</span>
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
  // channelBreachesThreshold 现在接受 thresholds 表作为参数（纯函数、可测试），
  // 不能直接当 Array.filter 的回调传——filter 会把 (index, array) 当成第二、
  // 三个参数塞给它，必须包一层箭头函数显式传 getThresholds()。
  const low = list.filter((q) => channelBreachesThreshold(q, getThresholds(), channels)).length;
  $("#summaryChips").innerHTML = `
    <span class="chip ok"><span class="chip-dot"></span>${t("chip.ok")} <b>${ok}</b></span>
    ${low ? `<span class="chip low"><span class="chip-dot"></span>${t("chip.low")} <b>${low}</b></span>` : ""}
    ${info ? `<span class="chip info"><span class="chip-dot"></span>${t("chip.info")} <b>${info}</b></span>` : ""}
    ${warn ? `<span class="chip warn"><span class="chip-dot"></span>${t("chip.warn")} <b>${warn}</b></span>` : ""}
    ${bad ? `<span class="chip bad"><span class="chip-dot"></span>${t("chip.bad")} <b>${bad}</b></span>` : ""}
    ${off ? `<span class="chip off"><span class="chip-dot"></span>${t("chip.off")} <b>${off}</b></span>` : ""}`;
}

/* 余额类渠道的金额展示：余额没有百分比概念，不画圆环——大号金额 + 说明文字。
   高度与窗口圆环行一致（106px），保证与其他卡片内容高度对齐。 */
function renderBalanceHero(amountObj) {
  const label = amountObj.label || `${amountObj.value ?? ""} ${amountObj.currency ?? ""}`.trim();
  const m = label.match(/^([^\d.,]+)([\d.,]+)$/); // 拆出货币符号（¥/$）与数字
  const symbol = m ? m[1] : "";
  const number = m ? m[2] : label;
  return `
    <div class="balance-hero">
      <div class="balance-value"><span class="balance-symbol">${esc(symbol)}</span><span class="balance-number">${esc(number)}</span></div>
      <div class="balance-label">${t("card.balanceLabel")}</div>
    </div>`;
}

function renderCard(q) {
  const meta = PROVIDER_META[q.type] || FALLBACK_META;
  const statusCls = STATUS_TAG_CLASS[q.status] || "not_found";
  const stText = statusText(q.status);
  const isDisabled = q.status === "disabled";
  const isAbnormal = !(q.status === "ok" || q.status === "info");
  const isLowAlert = channelBreachesThreshold(q, getThresholds(), channels);
  const typeLabel = esc(providerLabel(q.type));
  const manageUrl = safeUrl(
    providersCatalog[q.type]?.manage_url || channels.find((c) => c.id === q.id)?.base_url || ""
  );

  let body = "";
  if (q.status === "ok") {
    const parts = [];
    const hasPercentWindow = (q.windows || []).some(
      (w) => w.remaining_percent != null || w.used_percent != null
    );
    if (q.amount && !hasPercentWindow) {
      parts.push(renderBalanceHero(q.amount));
    }
    if (q.windows && q.windows.length && hasPercentWindow) {
      // 补齐标准的 3 个周期窗口（5小时 / 周 / 月），没有的补灰色未订阅/未提供占位槽
      const normalizedWindows = normalizeThreeWindows(q.windows);
      parts.push(renderWindows(normalizedWindows));
    }
    if (!parts.length) {
      parts.push(`<div class="card-amount"><span class="value placeholder">${t("card.noData")}</span></div>`);
    }
    body = `<div class="card-body">${parts.join("")}</div>`;
  } else if (q.status === "info") {
    body = `<div class="card-body"><div class="card-info-msg">${esc(q.message || t("card.noData"))}</div></div>`;
  } else if (isDisabled) {
    body = `<div class="card-body"><div class="card-amount"><span class="value placeholder">${t("card.disabled")}</span></div></div>`;
  } else {
    body = `<div class="card-body"><div class="card-amount"><span class="value placeholder">${t("card.noData")}</span></div></div>`;
  }

  // 底部：正常/提示状态优先展示凭据来源；异常状态展示错误信息。两者都有时互为提示 title。
  let footText, footTitle, footCls;
  if (!isAbnormal) {
    footText = q.source || q.message || "";
    footTitle = q.source && q.message ? `${q.source}\n${q.message}` : footText;
    footCls = "src";
  } else {
    footText = q.message || stText;
    footTitle = q.source ? `${footText}\n${t("card.source")}: ${q.source}` : footText;
    footCls = "msg";
  }
  const time = q.updated_at ? fmtTime(q.updated_at) : "";

  const showRefresh = !isDisabled;
  const actions = `
    <div class="card-actions">
      ${showRefresh ? `
      <button type="button" class="btn-icon card-action" data-action="refresh-one" data-id="${esc(q.id)}" title="${t("card.refreshHint")}">
        <svg viewBox="0 0 24 24" class="icon"><path fill="currentColor" d="M17.65 6.35A8 8 0 1 0 19.73 14h-2.08a6 6 0 1 1-1.42-5.93L13 11h7V4l-2.35 2.35z"/></svg>
      </button>` : ""}
      <button type="button" class="btn-icon card-action" data-action="edit-card" data-id="${esc(q.id)}" title="${t("card.editHint")}">
        <svg viewBox="0 0 24 24" class="icon"><path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
      </button>
      <label class="switch switch-sm" title="${isDisabled ? t("card.enableHint") : t("card.disableHint")}">
        <input type="checkbox" data-action="toggle-enabled" data-id="${esc(q.id)}" data-type="${esc(q.type)}" ${isDisabled ? "" : "checked"}>
        <span class="switch-track"><span class="switch-thumb"></span></span>
      </label>
    </div>`;

  // 状态圆点（绿=正常）从头部移到内容区（第二行）右上角——头部只保留状态文字徽章，
  // 状态指示和内容放在同一行，头部不再被右侧的圆点/徽章/链接/把手挤满。
  if (body.startsWith('<div class="card-body">')) {
    body = body.replace(
      '<div class="card-body">',
      `<div class="card-body"><span class="status-dot ${statusCls}" title="${esc(footTitle || stText)}"></span>`
    );
  }

  return `
    <article class="card${isDisabled ? " is-disabled" : ""}${isLowAlert ? " is-low-alert" : ""}" style="--card-accent:${meta.a}" data-id="${esc(q.id)}">
      <div class="card-head">
        ${iconTile(meta)}
        <div class="card-title">
          <div class="name">${esc(q.name)}</div>
          <div class="plan">${q.plan_name ? esc(q.plan_name) : typeLabel}</div>
        </div>
        <span class="status-tag ${statusCls}">${esc(stText)}</span>
        ${manageUrl ? `<a class="card-ext-link" href="${esc(manageUrl)}" target="_blank" rel="noopener" title="${t("card.managePageHint")}" aria-label="${t("card.managePageHint")}"><svg viewBox="0 0 24 24" class="icon"><path fill="currentColor" d="M14 3h7v7h-2V6.41l-9.29 9.3-1.42-1.42L17.59 5H14V3zM5 5h6v2H5v12h12v-6h2v8H3V5h2z"/></svg></a>` : ""}
        <span class="card-drag-handle" draggable="true" title="${t("card.dragHint")}" aria-label="${t("card.dragHint")}">⠿</span>
      </div>
      ${body}
      <div class="card-foot">
        <div class="foot-info">
          <span class="${footCls}" title="${esc(footTitle)}">${esc(footText)}</span>
          <span class="time">${esc(time)}</span>
        </div>
        ${actions}
      </div>
    </article>`;
}

function renderWindows(windows) {
  // 所有额度窗口统一渲染为进度条（.window-bar）模式，不再渲染为圆环
  return `<div class="windows-bar-list">${windows.map(renderBar).join("")}</div>`;
}

/* 卡片拖拽排序持久化：全局渠道 id 顺序数组，存 localStorage，刷新后保持 */
const CARD_ORDER_KEY = "quotaboard_prefs.card_order";

function loadCardOrder() {
  try {
    const order = JSON.parse(localStorage.getItem(CARD_ORDER_KEY) || "[]");
    if (Array.isArray(order)) return order;
  } catch {}
  return [];
}

function saveCardOrder(order) {
  try {
    localStorage.setItem(CARD_ORDER_KEY, JSON.stringify(order));
  } catch {}
}

function _cardRank(order, id) {
  const idx = order.indexOf(id);
  return idx === -1 ? 9999 : idx;
}

/* 拖拽结束（drop）：来源卡与目标卡交换顺序并持久化，然后重渲染应用新顺序。
   只允许同一分类内交换（卡片归属哪个分类由后端决定，前端不能改）。 */
function onDropCard(to) {
  const from = document.querySelector(".card.dragging");
  if (!from || from === to) return;
  if (from.closest("section") !== to.closest("section")) return;
  // 以当前 DOM 顺序为基准交换两张卡的位置，存 localStorage 后重渲染
  const domOrder = [...document.querySelectorAll(".card")].map((c) => c.dataset.id);
  const i = domOrder.indexOf(from.dataset.id);
  const j = domOrder.indexOf(to.dataset.id);
  [domOrder[i], domOrder[j]] = [domOrder[j], domOrder[i]];
  saveCardOrder(domOrder);
  renderDashboard();
}

/* ringColor() 在 renderBar 里逐个窗口调用（N 张卡 × M 个窗口），如果每次都
   getComputedStyle() 会是大量强制样式读取。这里改成每轮渲染只读一次、缓存到
   themeColorCache，renderDashboard() 开头负责刷新它（自然也覆盖了主题切换的场景）。 */
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

/* 窗口标签的本地化：
   优先用 labelKey（normalizeThreeWindows 生成的占位窗口）；否则按 window.key
   识别标准档位（five_hour/weekly/monthly 及其 agent_/coding_ 前缀变体）走翻译表；
   其余自定义窗口（如 "Gemini 2.5 Pro"、"已用比例"）是后端数据标签，保持原样。 */
function windowLabel(w) {
  if (w.labelKey) return t(w.labelKey);
  const key = w.key || "";
  const m = key.match(/^(agent|coding)_(five_hour|weekly|monthly)$/);
  if (m) return t(`tier.${m[1]}.${m[2]}`);
  if (["five_hour", "weekly", "monthly"].includes(key)) return t(`tier.${key}`);
  return w.label || key;
}

function renderBar(w) {
  if (noPercentData(w)) return renderBarFlat(w);
  const used = w.used_percent ?? (100 - w.remaining_percent);
  const remainingPct = Math.max(0, Math.min(100, w.remaining_percent ?? (100 - used)));
  const color = ringColor(remainingPct);

  const label = windowLabel(w);
  const maxLabel = w.maxLabelKey ? t(w.maxLabelKey) : w.max_label;
  // 展示格式：剩余 80% · 限额 10,000 APF · 重置 5 小时后
  const remainTxt = `${t("window.remaining")} ${remainingPct.toFixed(0)}%`;
  const maxTxt = maxLabel ? `${t("window.quota")} ${maxLabel}` : "";
  const usedTxt = w.used_label ? `${t("window.used")} ${w.used_label}` : "";
  const resetTxt = w.reset_at ? `${t("window.reset")} ${fmtResetText(fmtReset(w.reset_at))}` : "";
  const extra = [remainTxt, maxTxt, usedTxt, resetTxt].filter(Boolean).join(" · ");

  return `
    <div class="window-bar">
      <div class="bar-head">
        <span>${esc(label)}</span>
        <span class="bar-pct" style="color:${color}">${t("window.remaining")} ${remainingPct.toFixed(0)}%</span>
      </div>
      <div class="bar-track">
        <div class="bar-fill" style="--bar-c:${color};--bar-c2:${color};width:${remainingPct}%"></div>
      </div>
      <div class="bar-extra">${esc(extra)}</div>
    </div>`;
}

/* 没有百分比概念的窗口条目（列表形式）：不画进度条，只展示文本 */
function renderBarFlat(w) {
  const label = windowLabel(w);
  const maxLabel = w.maxLabelKey ? t(w.maxLabelKey) : w.max_label;
  const mainText = maxLabel || w.used_label || "";
  const resetText = w.reset_at ? `${t("window.reset")} ${fmtResetText(fmtReset(w.reset_at))}` : "";
  return `
    <div class="window-bar bar-flat">
      <div class="bar-head">
        <span>${esc(label)}</span>
        ${mainText ? `<span class="bar-flat-value">${esc(mainText)}</span>` : ""}
      </div>
      ${resetText ? `<div class="bar-extra">${esc(resetText)}</div>` : ""}
    </div>`;
}

/* 把 fmtReset 返回的 {key,value} 渲染成可读字符串 */
function fmtResetText(r) {
  if (r.key === "reset.now") return t("reset.now");
  return t(r.key, { n: r.value });
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
    // 但火山子卡的 id 带 _agent/_coding 后缀，后端会归一成配置 id 去查询，于是
    // 哪怕只点了一张子卡的刷新按钮，也可能一次性把同配置下的两张子卡都返回。
    // 必须把返回的每一张卡都合并进 quotas 并各自重绘，否则"多返回的那张卡"的
    // 新数据会被直接丢弃，兄弟卡还停在刷新前的旧值上（数据都查回来了却白白丢掉）。
    const data = await fetchQuotas(`/api/quotas?force=1&ids=${encodeURIComponent(id)}`);
    for (const fresh of data.channels) {
      // 局部替换这一条数据（数组先更新，DOM 补丁失败也不会丢数据）
      const idx = quotas.findIndex((c) => c.id === fresh.id);
      if (idx >= 0) quotas[idx] = fresh;
      else quotas.push(fresh);
      // 被点击的卡片已经有 DOM 引用（card）；一并返回的兄弟卡（如火山 coding 卡）
      // 需要重新按 id 查 DOM。局部替换（insertAdjacentHTML + remove）是为了避免
      // 全屏重绘导致其他卡片闪烁，这里对每张返回的卡都做同样的局部替换。
      const target = fresh.id === id ? card : document.querySelector(`.card[data-id="${CSS.escape(fresh.id)}"]`);
      if (target) {
        const newCard = renderCard(fresh);
        target.insertAdjacentHTML("afterend", newCard);
        target.remove();
      }
      // 找不到对应 DOM 节点（比如这张卡是首次出现）：数据已经写进 quotas 了，
      // 不强行插入 DOM，下一次全量 renderDashboard() 会自然带出它，不会丢数据。
    }
    renderSummaryChips(quotas);
  } catch (e) {
    toast(t("toast.refreshOneFailed", { msg: e.message }), "err");
  } finally {
    btn.disabled = false;
    btn.classList.remove("spinning");
    // 失败路径下卡片没有被局部替换（旧节点还留在 DOM 上），card-busy 必须在这里
    // 清掉，否则刷新一旦失败卡片会永久停留在 busy 视觉态，直到下一次全量重绘。
    // 成功路径下旧节点已被 detach，对它 classList.remove 是无害 no-op。
    card?.classList.remove("card-busy");
  }
}

/* 卡片上的编辑按钮：打开配置弹窗并直接进入该渠道的编辑模式。
   火山子渠道 id 带 _agent/_coding 后缀，需归一到真实 config id 再匹配 channels。 */
function onEditCard(cardId) {
  const baseId = canonicalChannelId(cardId, channels);
  const ch = channels.find((c) => c.id === baseId);
  if (!ch) {
    toast(t("toast.channelNotFound"), "err");
    return;
  }
  openConfigModal();
  fillForm(ch);
}

async function onToggleEnabled(el) {
  const id = el.dataset.id;
  const type = el.dataset.type;
  const nextEnabled = el.checked;
  // 火山渠道在前端拆成 <id>_agent / <id>_coding 两张卡，但底层共享同一个 config
  // 渠道（同一个 enabled 开关）。用户在一张子卡上点开关，网络往返期间（loadChannels
  // + refreshQuotas 重绘前）另一张兄弟卡的开关还是旧状态——如果此时去点它，会基于
  // 错误的 UI 状态发起请求，产生语义混乱的 toggle 序列。这里把同源（归一后同一
  // config id）所有兄弟卡的开关一起锁住，直到全量重绘带回正确状态。
  const baseId = canonicalChannelId(id, channels);
  const peerSwitches = document.querySelectorAll(
    `.card[data-id] [data-action="toggle-enabled"]`
  );
  const peers = [...peerSwitches].filter((s) => canonicalChannelId(s.dataset.id, channels) === baseId);
  for (const s of peers) s.disabled = true;
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
    toast(nextEnabled ? t("toast.channelEnabled") : t("toast.channelDisabled"), "ok");
    await loadChannels();
    await refreshQuotas(true);
  } catch (err) {
    // 失败回滚：把同源所有兄弟卡的勾选态翻回（它们在用户眼里是同一个开关）。
    for (const s of peers) {
      s.checked = !nextEnabled;
      s.disabled = false;
    }
    toast(t("toast.toggleFailed", { msg: err.message }), "err");
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
  const totals = source.totals || {};
  const fmtTokens = (n) => (n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(n ?? 0));
  const modelStats = source.model_stats || [];
  const hasSessions = modelStats.some((m) => m.sessions !== undefined);
  const counts = [];
  const countTypeLabel = hasSessions ? t("local.sessions") : t("local.messages");
  const countLabel = t(hasSessions ? "local.daysSessions" : "local.daysMessages", { days: days ?? "-" });

  const stats = `
    <div class="local-stats">
      <div class="local-stat"><div class="label">${countLabel}</div><div class="value">${hasSessions ? totals.sessions ?? "-" : totals.messages ?? "-"}</div></div>
      <div class="local-stat"><div class="label">${t("local.inputTokens")}</div><div class="value">${fmtTokens(totals.input ?? 0)}</div></div>
      <div class="local-stat"><div class="label">${t("local.outputTokens")}</div><div class="value">${fmtTokens(totals.output ?? 0)}</div></div>
      <div class="local-stat"><div class="label">${t("local.cacheReadTokens")}</div><div class="value">${fmtTokens(totals.cache_read ?? 0)}</div></div>
      <div class="local-stat"><div class="label">${t("local.cacheWriteTokens")}</div><div class="value">${fmtTokens(totals.cache_write ?? 0)}</div></div>
      ${totals.has_cost ? `<div class="local-stat"><div class="label">${t("local.estimatedCost")}</div><div class="value">$${(totals.cost ?? 0).toFixed(2)}</div></div>` : ""}
    </div>`;

  let table = `<div class="local-empty">${t("local.noSessions")}</div>`;
  if (modelStats.length) {
    const rows = modelStats
      .map((m) => `
        <tr>
          <td class="model">${esc(m.model)}</td>
          <td>${hasSessions ? m.sessions ?? 0 : m.messages ?? 0}</td>
          <td>${fmtTokens(m.input ?? 0)}</td>
          <td>${fmtTokens(m.output ?? 0)}</td>
          ${totals.has_cost ? `<td>$${(m.cost ?? 0).toFixed(2)}</td>` : ""}
        </tr>`)
      .join("");
    table = `
      <div class="local-table-wrap">
        <table class="local-table">
          <thead><tr><th>${t("local.model")}</th><th>${esc(countTypeLabel)}</th><th>${t("local.input")}</th><th>${t("local.output")}</th>${totals.has_cost ? `<th>${t("local.estimatedCost")}</th>` : ""}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  const note = source.path || source.message || "";
  return `
    <section class="local-section">
      <div class="section-head">
        <h2><span class="section-dot local"></span>${esc(source.label || source.key || categoryLabel("local"))}</h2>
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
  renderChannelCount();
}

function renderChannelCount() {
  const el = $("#channelsCountLabel");
  if (el) el.textContent = t("config.channelsCount", { count: channels.length });
}

/* 自动探测本机已登录的订阅 CLI（Claude / Gemini / Grok / Codex / Copilot），
   为尚未配置的类型自动创建渠道。服务启动时已自动跑过一次，这里给用户一个
   「装了新 CLI 后手动再探一次」的入口。 */
async function onAutoDetect() {
  const btn = $("#btnAutoDetect");
  if (btn) { btn.disabled = true; btn.textContent = t("config.detecting"); }
  try {
    const res = await fetch("/api/channels/auto-detect", { method: "POST" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const added = data.added || [];
    if (added.length) {
      // 按当前语言把探测到的类型名本地化，回退到后端给的 name
      const names = added.map((a) => providerLabel(a.type)).join("、");
      toast(t("toast.autoDetectFound", { count: added.length, names }), "ok");
      await loadChannels();
      await refreshQuotas(true);
    } else {
      toast(t("toast.autoDetectNone"), "info");
    }
  } catch (e) {
    toast(t("toast.autoDetectFailed", { msg: e.message }), "err");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = t("config.autoDetect"); }
  }
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
        .map(([id]) => `<option value="${esc(id)}">${esc(providerLabel(id))}</option>`)
        .join("");
      return `<optgroup label="${esc(categoryLabel(cat))}">${optsHtml}</optgroup>`;
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
      ? `<div class="field-hint">${t("hint.secretKeep")}${maskedVal ? "" : t("hint.secretNotSet")}</div>`
      : "";
    // 密钥字段加一个显示/隐藏切换按钮（小眼睛），方便用户核对输入的长 API Key
    const toggle = isSecret
      ? `<button type="button" class="pw-toggle" data-toggle="${key}" title="${t("hint.showHide")}" aria-label="${t("hint.showHideAria", { label })}" tabindex="-1">
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
  // OpenCode 渠道同样把 Cookie 存进 api_key 字段（opencode.ai 登录态），
  // 另需 workspace_id（wrk_xxx）。与 MiMo 同源：官网无 JSON API，靠 SSR 页面
  // + 浏览器 Cookie。
  const isOpenCode = type === "opencode_subscription";
  // 火山渠道的 AK/SK 需要在火山引擎 IAM 控制台创建，提示用户去哪拿
  const isVolcengine = type === "volcengine";

  for (const f of meta.fields) {
    if (f === "api_key") {
      if (isMimo) {
        fields += field("api_key", t("field.cookie"), t("field.mimoCookiePlaceholder"), true, "text");
      } else if (isOpenCode) {
        fields += field("api_key", t("field.cookie"), t("field.opencodeCookiePlaceholder"), true, "text");
      } else {
        fields += field("api_key", t("field.apiKey"), t("field.apiKeyPlaceholder"));
      }
    }
    else if (f === "workspace_id") fields += field("workspace_id", t("field.workspaceId"), t("field.workspaceIdPlaceholder"), false, "text");
    else if (f === "base_url") fields += field("base_url", t("field.baseUrl"), t("field.baseUrlPlaceholder"), true, "text");
    else if (f === "ak") fields += field("ak", t("field.ak"), t("field.akPlaceholder"), false, "text");
    else if (f === "sk") fields += field("sk", t("field.sk"), t("field.skPlaceholder"));
    else if (f === "region") fields += field("region", t("field.region"), t("field.regionPlaceholder"), false, "text");
    else if (f === "organization") fields += field("organization", t("field.organization"), "", false, "text");
    else if (f === "project") fields += field("project", t("field.project"), "", false, "text");
  }

  const hint = isMimo
    ? t("hint.mimo")
    : isOpenCode
      ? t("hint.opencode")
      : isVolcengine
        ? t("hint.volcengine")
        : meta.category === "subscription"
          ? t("hint.subscription")
          : meta.category === "local"
            ? t("hint.local")
            : t("hint.balance");

  // Codex 渠道：OAuth 在线登录（推荐）+ 可选上传 auth.json（多账号）。
  // OAuth 按钮：点一下打开 ChatGPT 登录页，授权后自动建/关联渠道，最省事。
  // auth.json 上传：已有 Codex CLI 登录态时直接上传 auth.json，省去重新 OAuth。
  let codexAuth = "";
  if (type === "codex_subscription") {
    const cur = existing?.extra?.codex_auth_file
      ? t("codex.currentUploaded", { file: existing.extra.codex_auth_file })
      : t("codex.currentLocal");
    codexAuth = `
      <div class="field-full oauth-section">
        <label>${t("codex.oauthLogin")}</label>
        <button type="button" class="btn btn-oauth" id="btnCodexOauth">
          <svg viewBox="0 0 24 24" class="icon" aria-hidden="true"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12c0 4.42 2.87 8.17 6.84 9.5.5.08.66-.23.66-.5v-1.69c-2.77.6-3.36-1.34-3.36-1.34-.46-1.16-1.11-1.47-1.11-1.47-.91-.62.07-.6.07-.6 1 .07 1.53 1.03 1.53 1.03.87 1.52 2.34 1.07 2.91.83.09-.65.35-1.09.63-1.34-2.22-.25-4.55-1.11-4.55-4.92 0-1.11.38-2 1.03-2.71-.1-.25-.45-1.29.1-2.64 0 0 .84-.27 2.75 1.02.79-.22 1.65-.33 2.5-.33.85 0 1.71.11 2.5.33 1.91-1.29 2.75-1.02 2.75-1.02.55 1.35.2 2.39.1 2.64.65.71 1.03 1.6 1.03 2.71 0 3.82-2.34 4.66-4.57 4.91.36.31.69.92.69 1.85V21c0 .27.16.59.67.5C19.14 20.16 22 16.42 22 12A10 10 0 0 0 12 2z"/></svg>
          <span>${t("codex.oauthBtn")}</span>
        </button>
        <div class="field-hint">${t("codex.oauthHint")}</div>
      </div>
      <div class="field-full">
        <label for="fCodexAuth">${t("codex.authJson")}</label>
        <div class="file-upload">
          <input type="file" id="fCodexAuth" accept=".json,application/json">
          <button type="button" class="file-upload-btn">
            <svg viewBox="0 0 24 24" class="icon" aria-hidden="true"><path fill="currentColor" d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
            <span>${t("codex.chooseFile")}</span>
          </button>
          <span class="file-upload-name" id="fCodexAuthName">${t("codex.noFile")}</span>
        </div>
        <div class="field-hint">${t("codex.authJsonHint", { current: cur })}</div>
      </div>`;
  }

  container.innerHTML = fields + codexAuth + `<div class="form-hint">${hint}</div>`;

  // Codex 文件选择后更新自定义 UI 的文件名显示
  const codexInput = $("#fCodexAuth");
  if (codexInput) {
    codexInput.addEventListener("change", () => {
      const nameEl = $("#fCodexAuthName");
      if (nameEl) {
        nameEl.textContent = codexInput.files?.[0]?.name || t("codex.noFile");
        nameEl.classList.toggle("is-set", !!codexInput.files?.[0]);
      }
    });
  }

  // Codex OAuth 按钮：发起 ChatGPT 在线授权
  const oauthBtn = $("#btnCodexOauth");
  if (oauthBtn) {
    oauthBtn.addEventListener("click", () => startCodexOAuth(oauthBtn));
  }
}

function resetForm() {
  editingId = null;
  $("#channelForm").reset();
  $("#fEnabled").checked = true;
  renderTypeSelect();
  $("#fType").value = "deepseek";
  renderDynamicFields();
  $("#btnFormSave").textContent = t("config.save");
  $("#btnFormReset").classList.add("hidden");
}

/* ── Codex OAuth 在线授权 ────────────────────────────────────
   流程：POST /start 拿 authorize URL → window.open 打开 OpenAI 登录页 →
   轮询 /poll 等 OAuth 完成的结果（回调端点换好 token、建好渠道后写入结果）→
   成功则刷新渠道列表和额度，失败则提示。 */

async function startCodexOAuth(btn) {
  const originalHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="oauth-spinner"></span><span>${t("codex.oauthPreparing")}</span>`;
  try {
    const res = await fetch("/api/auth/codex/start", { method: "POST" });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    const { authorize_url, state } = await res.json();

    // 打开授权窗口（浏览器能过 OpenAI 的 Cloudflare 挑战）
    const popup = window.open(authorize_url, "codex_oauth", "width=560,height=720");
    if (!popup) {
      // 浏览器拦截了弹窗——降级为当前页跳转，并在新标签完成
      toast(t("codex.popupBlocked"), "err");
      window.location.href = authorize_url;
      btn.disabled = false;
      btn.innerHTML = originalHtml;
      return;
    }

    btn.innerHTML = `<span class="oauth-spinner"></span><span>${t("codex.oauthWaiting")}</span>`;
    // 轮询结果（最长 5 分钟，每 2 秒一次）
    const deadline = Date.now() + 5 * 60 * 1000;
    let result = null;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 2000));
      try {
        const pollRes = await fetch(`/api/auth/codex/poll?state=${encodeURIComponent(state)}`);
        if (pollRes.ok) {
          const data = await pollRes.json();
          if (data.status !== "pending") { result = data; break; }
        }
      } catch { /* 轮询失败继续重试 */ }
    }

    if (!result) {
      toast(t("codex.oauthTimeout"), "err");
    } else if (result.status === "ok") {
      const email = result.email ? `（${result.email}）` : "";
      toast(t("codex.oauthSuccess", { email }), "ok");
      try { popup.close(); } catch { /* 窗口可能已关 */ }
      resetForm();
      await loadChannels();
      await refreshQuotas(true);
    } else {
      toast(t("codex.oauthFailed") + ": " + (result.message || t("card.noData")), "err");
    }
  } catch (err) {
    toast(t("toast.saveFailed", { msg: err.message }), "err");
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalHtml;
  }
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
  set("workspace_id", ch.workspace_id || "");
  $("#btnFormSave").textContent = t("config.update");
  $("#btnFormReset").classList.remove("hidden");
  // modal 本身是覆盖层（可滚动），滚 window 没有意义；把表单滚动到可见区域顶部
  $("#channelForm").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function onSaveChannel(e) {
  e.preventDefault();
  const type = $("#fType").value;
  const meta = providersCatalog[type];
  const isOpenCode = type === "opencode_subscription";
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

  const fieldLabel = (r) => {
    if (r === "api_key") return isOpenCode ? t("field.cookie") : t("field.apiKey");
    if (r === "ak") return t("field.ak");
    if (r === "sk") return t("field.sk");
    if (r === "base_url") return t("field.baseUrl");
    if (r === "workspace_id") return t("field.workspaceId");
    return r;
  };

  // base_url 始终必填；密钥类字段（api_key/ak/sk）只在"新建"时必填。
  // workspace_id 已改为自动探测，不再是必填字段。
  for (const r of ["base_url"]) {
    if (meta.fields.includes(r) && !payload[r]) {
      toast(t("toast.fillRequired", { field: fieldLabel(r) }), "err");
      return;
    }
  }
  if (!editingId) {
    for (const r of SECRET_FIELDS) {
      if (meta.fields.includes(r) && !payload[r]) {
        toast(t("toast.fillRequired", { field: fieldLabel(r) }), "err");
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
    const saved = await res.json();

    // Codex 渠道：保存后若选了 auth.json，上传并关联到该渠道（多账号）
    let uploadErr = null;
    const fileInput = $("#fCodexAuth");
    if (fileInput && fileInput.files && fileInput.files[0]) {
      try {
        const fileText = await fileInput.files[0].text();
        const upRes = await fetch(`/api/channels/${saved.id}/codex-credentials`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: fileText }),
        });
        if (!upRes.ok) {
          const err = await upRes.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${upRes.status}`);
        }
        toast(t("codex.credentialsUploaded"), "ok");
      } catch (err) {
        uploadErr = err.message;
      }
    }

    toast(editingId ? t("toast.channelUpdated") : t("toast.channelSaved"), "ok");
    if (uploadErr) toast(t("toast.uploadCredentialsFailed", { msg: uploadErr }), "err");
    resetForm();
    await loadChannels();
    await refreshQuotas(true);
  } catch (err) {
    toast(t("toast.saveFailed", { msg: err.message }), "err");
  }
}

function renderChannelsList() {
  const container = $("#channelsList");
  if (!channels.length) {
    container.innerHTML = `<div class="local-empty">${t("config.noChannelsYet")}</div>`;
    return;
  }
  container.innerHTML = channels
    .map((ch) => {
      const meta = PROVIDER_META[ch.type] || FALLBACK_META;
      // Codex 渠道若上传过 auth.json，来源显示上传的凭据文件
      const key = ch.api_key || ch.ak || (ch.type === "codex_subscription" && ch.extra?.codex_auth_file ? t("config.uploadedCredentials") : t("config.autoRead"));
      return `
        <div class="channel-row">
          ${iconTile(meta, "sm")}
          <div class="row-name">
            <div class="t">${esc(ch.name)} ${ch.enabled ? "" : `<span class="status-tag disabled">${t("status.disabled")}</span>`}</div>
            <div class="ty">${esc(providerLabel(ch.type))}</div>
          </div>
          <span class="row-key">${esc(key)}</span>
          <div class="row-actions">
            <button type="button" class="btn-icon" title="${t("config.editHint")}" data-action="edit-channel" data-id="${esc(ch.id)}">
              <svg viewBox="0 0 24 24" class="icon" style="width:15px;height:15px"><path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
            </button>
            <button type="button" class="btn-icon" title="${t("config.deleteHint")}" data-action="delete-channel" data-id="${esc(ch.id)}" style="color:var(--red)">
              <svg viewBox="0 0 24 24" class="icon" style="width:15px;height:15px"><path fill="currentColor" d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
            </button>
          </div>
        </div>`;
    })
    .join("");
}

async function deleteChannel(id) {
  const ch = channels.find((c) => c.id === id);
  if (!confirm(t("toast.deleteConfirm", { name: ch?.name || id }))) return;
  try {
    const res = await fetch(`/api/channels/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    toast(t("toast.channelDeleted"), "ok");
    await loadChannels();
    await refreshQuotas(true);
  } catch (e) {
    toast(t("toast.deleteFailed", { msg: e.message }), "err");
  }
}

/* ── 工具 ───────────────────────────────────────────────── */

function fmtTime(ms) {
  const d = new Date(ms);
  const now = Date.now();
  const diff = now - ms;
  if (diff < 60_000) return t("time.justNow");
  if (diff < 3600_000) return t("time.minutesAgo", { n: Math.floor(diff / 60_000) });
  return d.toLocaleTimeString(getLang() === "zh-CN" ? "zh-CN" : "en-US", { hour12: false, hour: "2-digit", minute: "2-digit" });
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* 渲染到 href/src 的 URL 必须限制 scheme。esc() 只转义 HTML 特殊字符，挡不住
   javascript: / data: 这类危险协议——它们经过 esc() 后不含任何需要转义的字符，
   原样输出到 href="javascript:..." 仍可被点击触发脚本执行。base_url 是用户自填、
   且会出现在"导出（脱敏）"配置里被分享，构成真实的投递路径，这里用 scheme 白名单
   做纵深防御：非 http/https 一律不输出到可点击链接。 */
function safeUrl(u) {
  const s = String(u ?? "").trim();
  return /^https?:\/\//i.test(s) ? s : "";
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
  const theme = loadPrefs().theme || "light"; // 默认浅色
  let dark;
  if (theme === "light") dark = false;
  else if (theme === "dark") dark = true;
  else dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.classList.toggle("dark", dark);
}

/* ── 语言切换响应 ─────────────────────────────────────── */

function setupLangReactivity() {
  // 点击语言按钮只切换存储与 <html lang>，重绘由 applyLang 统一执行，
  // 避免这里与 usage.js 分别派发同名事件造成重复监听（见下方注释）。
  window.addEventListener("quotax:lang-change", () => applyLang());
}

function applyLang() {
  document.documentElement.lang = getLang() === "zh-CN" ? "zh-CN" : "en";
  applyStaticI18n();
  // 重绘所有动态内容
  renderSummaryChips(quotas);
  if (dashboardLoadedOnce) renderDashboard();
  renderLocalUsage();
  updateAutoRefreshLabel();
  // 打开的弹窗同步重绘
  if (!$("#settingsModal").classList.contains("hidden")) openSettingsModal();
  if (!$("#configModal").classList.contains("hidden")) {
    renderTypeSelect();
    renderChannelsList();
    renderChannelCount();
    // 正在编辑时动态字段需要按当前语言重绘
    if (editingId) {
      const ch = channels.find((c) => c.id === editingId);
      if (ch) fillForm(ch);
    }
  }
  if (!$("#historyModal").classList.contains("hidden")) {
    openHistoryModal();
    loadHistory();
  }
  // 通知用量视图重绘（usage.js 监听同一个 window 事件）
  document.dispatchEvent(new CustomEvent("quotax:usage-lang-change"));
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
  const current = loadPrefs().theme || "light";
  $("#themeOptions").innerHTML = THEME_OPTIONS.map(
    (o) => `<button type="button" class="seg-btn${o.value === current ? " active" : ""}" data-theme-val="${o.value}">${t(o.labelKey)}</button>`
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
      (o) => `<button type="button" class="seg-btn${o.value === current ? " active" : ""}${!autoOn && o.value > 0 ? " dim" : ""}" data-refresh-val="${o.value}">${t(o.labelKey)}</button>`
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
      updateAutoRefreshLabel();
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
    container.innerHTML = `<div class="local-empty">${t("settings.noChannelsForThreshold")}</div>`;
    return;
  }
  const thresholds = getThresholds();
  container.innerHTML = channels
    .map((ch) => {
      const meta = PROVIDER_META[ch.type] || FALLBACK_META;
      const val = thresholds[ch.id] ?? "";
      return `
        <div class="threshold-row">
          ${iconTile(meta, "", "width:28px;height:28px;font-size:11px")}
          <div class="threshold-name">
            <div class="t">${esc(ch.name)}</div>
            <div class="ty">${esc(providerLabel(ch.type))}</div>
          </div>
          <div class="threshold-input-wrap">
            <span class="threshold-suffix">${t("settings.thresholdSuffix")}</span>
            <input type="number" min="0" max="100" placeholder="${DEFAULT_THRESHOLD}" value="${val}" data-threshold-id="${esc(ch.id)}">
            <span class="threshold-suffix">${t("settings.thresholdAlert")}</span>
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
    _downloadJSON(data, `quotax-${tag}-${date}.json`);
    toast(includeSecrets ? t("toast.exportFull") : t("toast.exportSafe"), "ok");
  } catch (e) {
    toast(t("toast.exportFailed", { msg: e.message }), "err");
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
    toast(t("toast.importInvalidJson", { msg: err.message }), "err");
    return;
  }
  const mode = confirm(
    t("toast.importModeTitle") + "\n\n" +
      t("toast.importModeMerge") + "\n" +
      t("toast.importModeReplace") + "\n\n" +
      t("toast.importModeRecommend")
  )
    ? "merge"
    : "replace";
  if (mode === "replace") {
    if (!confirm(t("toast.importReplaceConfirm"))) return;
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
    toast(t("toast.importSuccess", { mode: mode === "merge" ? t("toast.modeMerge") : t("toast.modeReplace"), count: result.count }), "ok");
    await loadChannels();
    await refreshQuotas(true);
    closeConfigModal();
  } catch (err) {
    toast(t("toast.importFailed", { msg: err.message }), "err");
  }
}

/* ── 历史趋势 ─────────────────────────────────────────── */

let historyCache = null;

function openHistoryModal() {
  $("#historyModal").classList.remove("hidden");
  // 渠道下拉用当前已配置的渠道填充
  const sel = $("#historyChannel");
  const current = sel.value;
  sel.innerHTML = `<option value="">${t("history.allChannels")}</option>` +
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
  container.innerHTML = `<div class="history-loading">${t("history.loading")}</div>`;
  try {
    const url = `/api/history?days=${days}${cid ? `&ids=${encodeURIComponent(cid)}` : ""}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    historyCache = await res.json();
    renderHistory(historyCache, Number(days));
  } catch (e) {
    container.innerHTML = `<div class="empty-state error-state"><div class="empty-icon">⚠️</div><p>${t("history.loadFailed", { msg: e.message })}</p></div>`;
  }
}

function renderHistory(data, days) {
  const container = $("#historyContent");
  const entries = Object.entries(data.channels || {});
  if (!entries.length || entries.every(([, recs]) => !recs.length)) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📈</div>
        <h3>${t("history.noDataTitle")}</h3>
        <p>${t("history.noDataDesc")}</p>
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
    return `<div class="history-card empty"><div class="history-head">${iconTile(meta, "sm")}<span>${esc(name)}</span></div><div class="history-empty">${t("history.noRecords")}</div></div>`;
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
      label: t("history.amount"),
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
    return `<div class="history-card"><div class="history-head">${iconTile(meta, "sm")}<span>${esc(name)}</span></div><div class="history-empty">${t("history.noDrawable")}</div></div>`;
  }

  return `<div class="history-card">
    <div class="history-head">
      ${iconTile(meta, "sm")}
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

/* 调试/测试专用钩子：改成 ES module 之后顶层函数不会再像经典 script 那样自动
   挂到 window 上，浏览器手动验证（比如没有真实后端数据时，往 quotas 里塞一份
   构造好的假数据走一遍完整渲染路径，检查 normalizeThreeWindows 修复后 Gemini
   那种三个 custom 窗口的卡片是否真的渲染出来）就没有入口了，所以显式暴露一个
   最小的调试接口。仅用于调试/校验，不是给业务代码调用的正式 API。 */
function setQuotas(list) {
  quotas = Array.isArray(list) ? list : [];
  dashboardLoadedOnce = true;
  renderDashboard();
}
window.__quotax = { renderDashboard, setQuotas };

init();
