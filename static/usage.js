// 用量统计深度分析看板（「用量统计」Tab）。
// 数据来自后端 /api/usage/* 系列（SQLite 持久化的增量采集结果）。
// 与 app.js 的额度视图正交：进入本视图时才首次加载，离开不销毁状态。
//
// 本文件是独立 ES module（与 app.js 作用域隔离），自带 $ / esc / fmtTime 等工具，
// 不依赖 app.js 暴露全局变量——避免加载顺序耦合。

import { t, getLang } from "./i18n.js?v=9";

const $ = (sel) => document.querySelector(sel);
const USAGE_DAYS_KEY = "quotaboard_prefs.usage_days";
let usageState = {
  loaded: false,        // 是否已加载过一次（避免重复拉取）
  days: 14,
  source: "",           // 空字符串 = 全部
  model: "",            // 空字符串 = 全部
  metric: "tokens",     // 模型分布排序度量: tokens | cost
  overview: null,
  trend: [],
  models: [],
  log: { rows: [], limit: 200, offset: 0, has_more: false },
  filters: { sources: [], models: [] },
  collecting: false,
};
let usageRequestRevision = 0;
let usageAbortController = null;

function beginUsageRequest() {
  usageRequestRevision += 1;
  if (usageAbortController) usageAbortController.abort();
  const controller = new AbortController();
  usageAbortController = controller;
  return {
    revision: usageRequestRevision,
    signal: controller.signal,
    days: usageState.days,
    source: usageState.source,
    model: usageState.model,
    metric: usageState.metric,
  };
}

function isCurrentUsageRequest(ctx) {
  return Boolean(ctx) && ctx.revision === usageRequestRevision && !ctx.signal.aborted;
}

function usageParams(ctx, extra = {}) {
  const p = new URLSearchParams({ days: ctx.days, ...extra });
  if (ctx.source) p.set("source", ctx.source);
  if (ctx.model) p.set("model", ctx.model);
  return p;
}

function loadUsagePrefs() {
  try {
    const prefs = JSON.parse(localStorage.getItem("quotaboard_prefs") || "{}");
    usageState.days = prefs.usage_days || 14;
  } catch { /* noop */ }
}
function saveUsagePrefs() {
  try {
    const prefs = JSON.parse(localStorage.getItem("quotaboard_prefs") || "{}");
    prefs.usage_days = usageState.days;
    localStorage.setItem("quotaboard_prefs", JSON.stringify(prefs));
  } catch { /* noop */ }
}

// 监听视图切换：进入用量视图时首次加载
document.addEventListener("quotax:view-change", (e) => {
  if (e.detail.view === "usage") {
    if (!usageState.loaded) {
      loadUsagePrefs();
      loadAll();
    } else {
      renderUsage();
    }
  }
});

// 语言切换时重绘用量视图（由 app.js 的 applyLang 统一派发，避免重复监听）
window.addEventListener("quotax:usage-lang-change", () => {
  if (usageState.loaded) renderUsage();
});

async function loadAll() {
  const ctx = beginUsageRequest();
  renderUsageSkeleton();
  await Promise.all([
    loadOverview(ctx),
    loadTrend(ctx),
    loadModels(ctx),
    loadLog({}, ctx),
    loadFilters(ctx),
  ]);
  if (!isCurrentUsageRequest(ctx)) return;
  // 首次加载如果发现没数据，自动触发一次采集（可能服务刚启动、采集还在后台
  // 跑，或本机 CLI 日志还没被扫过）。采集是幂等的，重复跑不会产生重复记录。
  const ov = usageState.overview;
  if (ov && !ov.total_tokens && !ov.requests) {
    await collectNow();
    return;
  }
  usageState.loaded = true;
  renderUsage();
}

async function loadOverview(ctx = beginUsageRequest()) {
  try {
    const p = usageParams(ctx);
    const res = await fetch(`/api/usage/overview?${p}`, { signal: ctx.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (isCurrentUsageRequest(ctx)) usageState.overview = data;
  } catch (e) {
    if (e?.name !== "AbortError" && isCurrentUsageRequest(ctx)) usageState.overview = null;
  }
}

async function loadTrend(ctx = beginUsageRequest()) {
  try {
    const p = usageParams(ctx);
    const res = await fetch(`/api/usage/trend?${p}`, { signal: ctx.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (isCurrentUsageRequest(ctx)) usageState.trend = data;
  } catch (e) {
    if (e?.name !== "AbortError" && isCurrentUsageRequest(ctx)) usageState.trend = [];
  }
}

async function loadModels(ctx = beginUsageRequest()) {
  try {
    const p = usageParams(ctx, { metric: ctx.metric });
    const res = await fetch(`/api/usage/models?${p}`, { signal: ctx.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (isCurrentUsageRequest(ctx)) usageState.models = data;
  } catch (e) {
    if (e?.name !== "AbortError" && isCurrentUsageRequest(ctx)) usageState.models = [];
  }
}

async function loadLog({ append = false } = {}, ctx = beginUsageRequest()) {
  try {
    const currentRows = usageState.log?.rows || [];
    const offset = append ? currentRows.length : 0;
    const p = usageParams(ctx, { limit: "200", offset: String(offset) });
    const res = await fetch(`/api/usage/log?${p}`, { signal: ctx.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const page = await res.json();
    if (!isCurrentUsageRequest(ctx)) return;
    usageState.log = append
      ? { ...page, rows: [...currentRows, ...(page.rows || [])], offset: offset }
      : page;
  } catch (e) {
    if (e?.name !== "AbortError" && !append && isCurrentUsageRequest(ctx)) {
      usageState.log = { rows: [], limit: 200, offset: 0, has_more: false };
    }
  }
}

async function loadFilters(ctx = beginUsageRequest()) {
  try {
    const res = await fetch(`/api/usage/filters?days=${ctx.days}`, { signal: ctx.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (isCurrentUsageRequest(ctx)) usageState.filters = data;
  } catch (e) {
    if (e?.name !== "AbortError" && isCurrentUsageRequest(ctx)) {
      usageState.filters = { sources: [], models: [] };
    }
  }
}

async function collectNow() {
  if (usageState.collecting) return;
  usageState.collecting = true;
  const ctx = beginUsageRequest();
  const btn = $("#usageCollectBtn");
  if (btn) { btn.disabled = true; btn.textContent = t("usage.collecting"); }
  try {
    const res = await fetch("/api/usage/collect", { method: "POST", signal: ctx.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (!isCurrentUsageRequest(ctx)) return;
    await reloadAll();
  } catch (e) {
    if (e?.name !== "AbortError") toast(t("usage.collectFailed", { msg: e.message }), "err");
  } finally {
    usageState.collecting = false;
    if (btn) { btn.disabled = false; btn.textContent = t("usage.collect"); }
  }
}

function toast(msg, kind) {
  // 复用 app.js 的 toast（如果存在），否则简单 console；透传 kind 让错误样式生效
  if (typeof window.toast === "function") window.toast(msg, kind);
  else console.warn(msg);
}

// ── 渲染 ──────────────────────────────────────────────────────

function fmtTokens(n) {
  n = Number(n || 0);
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}
function fmtCost(n) {
  n = Number(n || 0);
  if (n === 0) return "$0";
  if (n < 0.01) return "$" + n.toFixed(4);
  return "$" + n.toFixed(2);
}
function fmtPct(n) {
  return (Number(n || 0) * 100).toFixed(1) + "%";
}
function fmtTime(ms) {
  if (typeof window.fmtTime === "function") return window.fmtTime(ms);
  const d = new Date(Number(ms));
  return d.toLocaleString(getLang() === "zh-CN" ? "zh-CN" : "en-US", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function esc(s) {
  if (typeof window.esc === "function") return window.esc(s);
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const SOURCE_LABELS = {
  claude_code: "Claude Code",
  codex: "Codex",
  gemini_cli: "Gemini CLI",
  grok_cli: "Grok CLI",
  opencode: "OpenCode",
};
function sourceLabel(s) { return SOURCE_LABELS[s] || s; }

function renderUsageSkeleton() {
  const v = $("#usageView");
  if (!v) return;
  v.innerHTML = `<div class="usage-loading">${t("usage.loading")}</div>`;
}

function renderUsage() {
  const v = $("#usageView");
  if (!v) return;
  const o = usageState.overview || {};
  const parts = [
    renderControls(),
    renderOverviewCards(o),
    renderTrendChart(),
    renderModelBreakdown(),
    renderRequestLog(),
    renderPricingNote(),
  ];
  v.innerHTML = parts.join("");
  bindUsageEvents();
}

function renderControls() {
  const daysOpts = [7, 14, 30, 90].map(
    (d) => `<option value="${d}" ${d === usageState.days ? "selected" : ""}>${t("history.days" + d)}</option>`
  ).join("");
  const srcOpts = [`<option value="">${t("usage.allSources")}</option>`]
    .concat((usageState.filters.sources || []).map(
      (s) => `<option value="${esc(s.source)}" ${s.source === usageState.source ? "selected" : ""}>${esc(sourceLabel(s.source))} (${s.records})</option>`
    )).join("");
  const allModels = usageState.filters.models || [];
  const selectedModel = usageState.model;
  const modelList = allModels.slice(0, 50);
  if (selectedModel && !modelList.includes(selectedModel) && allModels.includes(selectedModel)) {
    modelList.push(selectedModel);
  }
  const modelOpts = [`<option value="">${t("usage.allModels")}</option>`]
    .concat(modelList.map(
      (m) => `<option value="${esc(m)}" ${m === selectedModel ? "selected" : ""}>${esc(m)}</option>`
    )).join("");
  return `
    <div class="usage-controls">
      <label>${t("usage.time")} <select id="usageDays">${daysOpts}</select></label>
      <label>${t("usage.source")} <select id="usageSource">${srcOpts}</select></label>
      <label>${t("usage.model")} <select id="usageModel">${modelOpts}</select></label>
      <button class="btn btn-primary usage-collect-btn" id="usageCollectBtn">${t("usage.collect")}</button>
    </div>`;
}

function renderOverviewCards(o) {
  const total = o.total_tokens || 0;
  const inp = o.input || 0, outp = o.output || 0, cc = o.cache_creation || 0, cr = o.cache_read || 0;
  // 四桶占比条（cache_read 高亮——缓存命中省钱）
  const pct = (x) => (total > 0 ? (x / total * 100) : 0);
  const bar = (label, val, color) => `
    <div class="usage-bar-row">
      <span class="usage-bar-label">${label}</span>
      <div class="usage-bar-track"><div class="usage-bar-fill" style="width:${pct(val)}%;background:${color}"></div></div>
      <span class="usage-bar-val">${fmtTokens(val)}</span>
    </div>`;
  return `
    <div class="usage-overview">
      <div class="usage-kpi">
        <div class="usage-kpi-label">${t("usage.totalTokens")}</div>
        <div class="usage-kpi-value">${fmtTokens(total)}</div>
        <div class="usage-kpi-sub">${o.requests || 0} ${t("usage.requests")} · ${o.sessions || 0} ${t("usage.sessions")}</div>
      </div>
      <div class="usage-kpi">
        <div class="usage-kpi-label">${t("usage.cacheHitRate")}</div>
        <div class="usage-kpi-value ${o.cache_hit_rate >= 0.5 ? "kpi-good" : ""}">${fmtPct(o.cache_hit_rate)}</div>
        <div class="usage-kpi-sub">${t("usage.cacheHitFormula")}</div>
      </div>
      <div class="usage-kpi">
        <div class="usage-kpi-label">${t("usage.estimatedCost")}</div>
        <div class="usage-kpi-value">${o.has_cost ? fmtCost(o.cost) : "—"}</div>
        <div class="usage-kpi-sub">${o.has_cost ? t("usage.pricingEstimated") : t("usage.noPricing")}</div>
      </div>
      <div class="usage-token-bars">
        ${bar(t("usage.input"), inp, "var(--accent)")}
        ${bar(t("usage.output"), outp, "#10b981")}
        ${bar(t("usage.cacheRead"), cr, "#8b5cf6")}
        ${bar(t("usage.cacheWrite"), cc, "#f59e0b")}
      </div>
    </div>`;
}

// ── 趋势图（纯 SVG，按天 4 桶折线，参考 VaultOne Usage Trend Chart）──
function renderTrendChart() {
  const data = usageState.trend || [];
  if (!data.length) return `<div class="usage-section"><h3>${t("usage.trendTitle")}</h3><div class="usage-empty">${t("usage.noData")}</div></div>`;
  const W = 760, H = 200, PAD = { l: 50, r: 16, t: 16, b: 28 };
  const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const maxV = Math.max(1, ...data.flatMap((d) => [d.input, d.output, d.cache_creation, d.cache_read]));
  const x = (i) => PAD.l + (data.length <= 1 ? iw / 2 : (i / (data.length - 1)) * iw);
  const y = (v) => PAD.t + ih - (v / maxV) * ih;

  const series = [
    { key: "input", color: "var(--accent)", label: t("usage.input") },
    { key: "output", color: "#10b981", label: t("usage.output") },
    { key: "cache_read", color: "#8b5cf6", label: t("usage.cacheRead") },
    { key: "cache_creation", color: "#f59e0b", label: t("usage.cacheWrite") },
  ];
  const lines = series.map((s) => {
    const pts = data.map((d, i) => `${x(i)},${y(d[s.key] || 0)}`).join(" ");
    return `<polyline class="usage-line" points="${pts}" fill="none" stroke="${s.color}" stroke-width="1.8"/>`;
  }).join("");
  // X 轴标签：太密就隔几个显示
  const step = Math.ceil(data.length / 8);
  const xlabels = data.map((d, i) =>
    i % step === 0 ? `<text x="${x(i)}" y="${H - 8}" class="usage-axis-label" text-anchor="middle">${d.day.slice(5)}</text>` : ""
  ).join("");
  // Y 轴标签
  const ylabels = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const val = Math.round(maxV * f);
    return `<text x="${PAD.l - 6}" y="${y(maxV * f) + 4}" class="usage-axis-label" text-anchor="end">${fmtTokens(val)}</text>`;
  }).join("");
  const legend = series.map((s) =>
    `<span class="usage-legend-item"><i style="background:${s.color}"></i>${s.label}</span>`
  ).join("");
  return `
    <div class="usage-section">
      <h3>${t("usage.trendTitle")} <span class="usage-legend">${legend}</span></h3>
      <div class="usage-chart-wrap">
        <svg viewBox="0 0 ${W} ${H}" class="usage-chart" preserveAspectRatio="xMidYMid meet">
          ${ylabels}${xlabels}${lines}
        </svg>
      </div>
    </div>`;
}

// ── 模型分布（Top 8 + 其他，支持 token / cost 切换）──
function renderModelBreakdown() {
  const data = usageState.models || [];
  if (!data.length) return `<div class="usage-section"><h3>${t("usage.modelDistribution")}</h3><div class="usage-empty">${t("usage.noData")}</div></div>`;
  const metricLabel = usageState.metric === "cost" ? t("usage.metricCost") : t("usage.metricToken");
  const total = data.reduce((a, m) => a + (usageState.metric === "cost" ? m.cost : m.tokens), 0);
  const maxVal = Math.max(1, ...data.map((m) => (usageState.metric === "cost" ? m.cost : m.tokens)));
  const rows = data.map((m) => {
    const val = usageState.metric === "cost" ? m.cost : m.tokens;
    const pct = total > 0 ? (val / total * 100) : 0;
    const valStr = usageState.metric === "cost" ? fmtCost(m.cost) : fmtTokens(m.tokens);
    // is_other 行：展示文案按当前语言本地化（"其他 (N)" / "Other (N)"），
    // data-model 用空串使其不可被点击筛选——聚合行没有对应真实模型 key。
    const name = m.is_other ? t("usage.otherLabel", { count: m.other_count ?? 0 }) : esc(m.model);
    const selectable = m.is_other ? "" : `data-model="${esc(m.model)}"`;
    return `
      <div class="usage-model-row ${m.model === usageState.model ? "active" : ""}${m.is_other ? " is-other" : ""}" ${selectable}>
        <span class="usage-model-name">${name}</span>
        <div class="usage-model-bar"><div style="width:${(val / maxVal * 100)}%"></div></div>
        <span class="usage-model-val">${valStr}</span>
        <span class="usage-model-pct">${pct.toFixed(1)}%</span>
      </div>`;
  }).join("");
  return `
    <div class="usage-section">
      <h3>${t("usage.modelDistribution")}
        <span class="usage-metric-toggle">
          <button class="${usageState.metric === "tokens" ? "active" : ""}" data-metric="tokens">${t("usage.metricToken")}</button>
          <button class="${usageState.metric === "cost" ? "active" : ""}" data-metric="cost">${t("usage.metricCost")}</button>
        </span>
      </h3>
      <div class="usage-model-list">${rows}</div>
      <div class="usage-hint">${t("usage.filterHint", { metric: metricLabel })}</div>
    </div>`;
}

// ── 逐请求日志表 ──
function renderRequestLog() {
  const rows = (usageState.log && usageState.log.rows) || [];
  if (!rows.length) return `<div class="usage-section"><h3>${t("usage.requestLog")}</h3><div class="usage-empty">${t("usage.noData")}</div></div>`;
  const stopClass = (r) => {
    if (!r) return "";
    if (["end_turn", "stop"].includes(r)) return "stop-ok";
    if (["tool_use", "tool_calls"].includes(r)) return "stop-tool";
    if (["max_tokens", "context_window"].includes(r)) return "stop-warn";
    if (["refusal", "error"].includes(r)) return "stop-err";
    return "";
  };
  const body = rows.map((r) => `
    <tr>
      <td class="log-time">${fmtTime(r.timestamp_ms)}</td>
      <td class="log-src">${esc(sourceLabel(r.source))}</td>
      <td class="log-model">${esc(r.model)}</td>
      <td class="num">${fmtTokens(r.input)}</td>
      <td class="num">${fmtTokens(r.output)}</td>
      <td class="num">${fmtTokens(r.cache_creation)}</td>
      <td class="num">${fmtTokens(r.cache_read)}</td>
      <td class="num total">${fmtTokens(r.total_tokens)}</td>
      <td class="num">${r.has_cost ? fmtCost(r.cost) : "—"}</td>
      <td class="log-stop ${stopClass(r.stop_reason)}">${esc(r.stop_reason || "—")}</td>
    </tr>`).join("");
  return `
    <div class="usage-section">
      <h3>${t("usage.requestLog")} <span class="usage-hint">${t("usage.recentRows", { count: rows.length })}</span></h3>
      <div class="usage-table-wrap">
        <table class="usage-table">
          <thead><tr>
            <th>${t("usage.timeCol")}</th><th>${t("usage.sourceCol")}</th><th>${t("usage.modelCol")}</th>
            <th>${t("usage.inputCol")}</th><th>${t("usage.outputCol")}</th><th>${t("usage.cacheWriteCol")}</th><th>${t("usage.cacheReadCol")}</th>
            <th>${t("usage.totalCol")}</th><th>${t("usage.costCol")}</th><th>${t("usage.stopReasonCol")}</th>
          </tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>
      ${usageState.log?.has_more ? `<div class="usage-load-more-wrap"><button class="btn btn-ghost" id="usageLogMoreBtn">${t("usage.loadMore")}</button></div>` : ""}
    </div>`;
}

function renderPricingNote() {
  return `
    <div class="usage-section usage-pricing-section">
      <h3>${t("usage.pricingAndCost")}</h3>
      <div class="usage-pricing-actions">
        <button class="btn btn-ghost" id="usageLitellmBtn">${t("usage.updateLitellm")}</button>
        <button class="btn btn-ghost" id="usageRebillBtn">${t("usage.rebill")}</button>
        <button class="btn btn-ghost" id="usagePricingBtn">${t("usage.viewPricing")}</button>
        <span class="usage-hint">${t("usage.costDisclaimer")}</span>
      </div>
      <div id="usagePricingPanel" class="hidden"></div>
    </div>`;
}

// ── 事件绑定 ──

function bindUsageEvents() {
  const days = $("#usageDays");
  if (days) days.addEventListener("change", (e) => {
    usageState.days = Number(e.target.value);
    saveUsagePrefs();
    reloadAll();
  });
  const src = $("#usageSource");
  if (src) src.addEventListener("change", (e) => {
    usageState.source = e.target.value;
    reloadAll();
  });
  const mdl = $("#usageModel");
  if (mdl) mdl.addEventListener("change", (e) => {
    usageState.model = e.target.value;
    reloadAll();
  });
  const collect = $("#usageCollectBtn");
  if (collect) collect.addEventListener("click", collectNow);

  // 模型分布：点击行按模型筛选（聚合行无 data-model，不响应点击）
  document.querySelectorAll(".usage-model-row").forEach((row) => {
    row.addEventListener("click", () => {
      const m = row.dataset.model;
      if (!m) return;
      usageState.model = (usageState.model === m) ? "" : m;
      reloadAll();
    });
  });
  // token / cost 切换
  document.querySelectorAll(".usage-metric-toggle button").forEach((btn) => {
    btn.addEventListener("click", () => {
      usageState.metric = btn.dataset.metric;
      reloadAll();
    });
  });
  // 定价操作
  const litellm = $("#usageLitellmBtn");
  if (litellm) litellm.addEventListener("click", updateLitellm);
  const rebill = $("#usageRebillBtn");
  if (rebill) rebill.addEventListener("click", doRebill);
  const pricing = $("#usagePricingBtn");
  if (pricing) pricing.addEventListener("click", togglePricingPanel);
  const more = $("#usageLogMoreBtn");
  if (more) more.addEventListener("click", async () => {
    more.disabled = true;
    more.textContent = t("usage.loadingMore");
    const ctx = beginUsageRequest();
    await loadLog({ append: true }, ctx);
    if (isCurrentUsageRequest(ctx)) renderUsage();
  });
}

async function reloadAll() {
  const ctx = beginUsageRequest();
  await Promise.all([
    loadOverview(ctx),
    loadTrend(ctx),
    loadModels(ctx),
    loadLog({}, ctx),
    loadFilters(ctx),
  ]);
  if (!isCurrentUsageRequest(ctx)) return;
  usageState.loaded = true;
  renderUsage();
}

async function updateLitellm() {
  const btn = $("#usageLitellmBtn");
  if (btn) { btn.disabled = true; btn.textContent = t("usage.updating"); }
  try {
    const res = await fetch("/api/usage/pricing/litellm", { method: "POST" });
    const data = await res.json();
    if (data.error) toast(t("usage.updateLitellmFailed", { msg: data.error }));
    else {
      toast(t("usage.updateLitellmSuccess", { count: data.updated }));
      await doRebill(true); // 用新单价补算
    }
  } catch (e) { toast(t("usage.updateLitellmFailed", { msg: e.message })); }
  finally { if (btn) { btn.disabled = false; btn.textContent = t("usage.updateLitellm"); } }
}

async function doRebill(silent) {
  const btn = $("#usageRebillBtn");
  if (btn) { btn.disabled = true; btn.textContent = t("usage.rebilling"); }
  try {
    const res = await fetch("/api/usage/pricing/rebill", { method: "POST" });
    const data = await res.json();
    if (!silent) toast(t("usage.rebillResult", { count: data.recounted, zero: data.still_zero }));
    await reloadAll();
  } catch (e) { if (!silent) toast(t("usage.rebillFailed", { msg: e.message })); }
  finally { if (btn) { btn.disabled = false; btn.textContent = t("usage.rebill"); } }
}

async function togglePricingPanel() {
  const panel = $("#usagePricingPanel");
  if (!panel) return;
  if (!panel.classList.contains("hidden")) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
    return;
  }
  panel.classList.remove("hidden");
  panel.innerHTML = `<div class="usage-loading">${t("usage.pricingLoading")}</div>`;
  try {
    const res = await fetch("/api/usage/pricing");
    const data = await res.json();
    renderPricingTable(panel, data.entries || []);
  } catch (e) {
    // 错误信息可能来自网络/上游响应，不能未经转义直接拼进 innerHTML。
    panel.innerHTML = `<div class="usage-empty">${t("usage.pricingLoadFailed", { msg: esc(e.message) })}</div>`;
  }
}

function renderPricingTable(container, entries) {
  if (!entries.length) {
    container.innerHTML = `<div class="usage-empty">${t("usage.pricingEmpty")}</div>`;
    return;
  }
  const rows = entries.map((e) => `
    <tr data-key="${esc(e.model_key)}">
      <td class="model">${esc(e.model_key)}${e.is_builtin ? `<span class="pricing-tag">${t("usage.pricingBuiltin")}</span>` : ""}</td>
      <td class="num">${e.input_per_million}</td>
      <td class="num">${e.output_per_million}</td>
      <td class="num">${e.cache_read_per_million}</td>
      <td class="num">${e.cache_creation_per_million}</td>
      <td>${e.can_restore_default
        ? `<button class="btn btn-ghost btn-sm" data-restore="${esc(e.model_key)}">${t("usage.pricingRestore")}</button>`
        : (e.is_builtin ? "" : `<button class="btn btn-ghost btn-sm" data-del="${esc(e.model_key)}">${t("usage.pricingDelete")}</button>`)}</td>
    </tr>`).join("");
  container.innerHTML = `
    <div class="usage-table-wrap">
      <table class="usage-table pricing-table">
        <thead><tr>
          <th>${t("usage.pricingModel")}</th><th>${t("usage.pricingInput")}</th><th>${t("usage.pricingOutput")}</th>
          <th>${t("usage.pricingCacheRead")}</th><th>${t("usage.pricingCacheWrite")}</th><th></th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <details class="pricing-add">
      <summary>${t("usage.pricingAdd")}</summary>
      <div class="pricing-add-form">
        <input id="pk" placeholder="${t("usage.pricingModelKeyPlaceholder")}">
        <input id="pin" type="number" step="0.01" placeholder="${t("usage.pricingInputPlaceholder")}">
        <input id="pout" type="number" step="0.01" placeholder="${t("usage.pricingOutputPlaceholder")}">
        <input id="pcr" type="number" step="0.01" placeholder="${t("usage.pricingCacheReadPlaceholder")}">
        <input id="pcc" type="number" step="0.01" placeholder="${t("usage.pricingCacheWritePlaceholder")}">
        <button class="btn btn-primary btn-sm" id="pSave">${t("usage.pricingSave")}</button>
      </div>
    </details>`;
  // 删除
  container.querySelectorAll("[data-del]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const key = btn.dataset.del;
      await fetch(`/api/usage/pricing/${encodeURIComponent(key)}`, { method: "DELETE" });
      togglePricingPanel(); togglePricingPanel(); // 重新加载
    });
  });
  container.querySelectorAll("[data-restore]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const key = btn.dataset.restore;
      const res = await fetch(`/api/usage/pricing/${encodeURIComponent(key)}/restore`, { method: "POST" });
      if (!res.ok) { toast(t("usage.pricingSaveFailed")); return; }
      toast(t("usage.pricingRestore"));
      togglePricingPanel(); togglePricingPanel();
    });
  });
  // 新增 / 覆盖
  const save = container.querySelector("#pSave");
  if (save) save.addEventListener("click", async () => {
    const payload = {
      model_key: container.querySelector("#pk").value.trim(),
      input_per_million: Number(container.querySelector("#pin").value || 0),
      output_per_million: Number(container.querySelector("#pout").value || 0),
      cache_read_per_million: Number(container.querySelector("#pcr").value || 0),
      cache_creation_per_million: Number(container.querySelector("#pcc").value || 0),
    };
    if (!payload.model_key) { toast(t("usage.pricingModelKeyRequired")); return; }
    const res = await fetch("/api/usage/pricing", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (res.ok) {
      toast(t("usage.pricingSaved"));
      await doRebill(true);
      togglePricingPanel(); togglePricingPanel();
    } else { toast(t("usage.pricingSaveFailed")); }
  });
}
