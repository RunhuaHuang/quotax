// 用量统计深度分析看板（「用量统计」Tab）。
// 数据来自后端 /api/usage/* 系列（SQLite 持久化的增量采集结果）。
// 与 app.js 的额度视图正交：进入本视图时才首次加载，离开不销毁状态。
//
// 本文件是独立 ES module（与 app.js 作用域隔离），自带 $ / esc / fmtTime 等工具，
// 不依赖 app.js 暴露全局变量——避免加载顺序耦合。

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
  log: null,
  filters: { sources: [], models: [] },
  collecting: false,
};

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

async function loadAll() {
  renderUsageSkeleton();
  await Promise.all([
    loadOverview(),
    loadTrend(),
    loadModels(),
    loadLog(),
    loadFilters(),
  ]);
  usageState.loaded = true;
  renderUsage();
}

async function loadOverview() {
  try {
    const p = new URLSearchParams({ days: usageState.days });
    if (usageState.source) p.set("source", usageState.source);
    if (usageState.model) p.set("model", usageState.model);
    const res = await fetch(`/api/usage/overview?${p}`);
    usageState.overview = await res.json();
  } catch (e) { usageState.overview = null; }
}

async function loadTrend() {
  try {
    const p = new URLSearchParams({ days: usageState.days });
    if (usageState.source) p.set("source", usageState.source);
    if (usageState.model) p.set("model", usageState.model);
    const res = await fetch(`/api/usage/trend?${p}`);
    usageState.trend = await res.json();
  } catch { usageState.trend = []; }
}

async function loadModels() {
  try {
    const p = new URLSearchParams({ days: usageState.days, metric: usageState.metric });
    if (usageState.source) p.set("source", usageState.source);
    const res = await fetch(`/api/usage/models?${p}`);
    usageState.models = await res.json();
  } catch { usageState.models = []; }
}

async function loadLog() {
  try {
    const p = new URLSearchParams({ days: usageState.days, limit: "200" });
    if (usageState.source) p.set("source", usageState.source);
    if (usageState.model) p.set("model", usageState.model);
    const res = await fetch(`/api/usage/log?${p}`);
    usageState.log = await res.json();
  } catch { usageState.log = { rows: [] }; }
}

async function loadFilters() {
  try {
    const res = await fetch(`/api/usage/filters?days=${usageState.days}`);
    usageState.filters = await res.json();
  } catch { usageState.filters = { sources: [], models: [] }; }
}

async function collectNow() {
  if (usageState.collecting) return;
  usageState.collecting = true;
  const btn = $("#usageCollectBtn");
  if (btn) { btn.disabled = true; btn.textContent = "采集中…"; }
  try {
    await fetch("/api/usage/collect", { method: "POST" });
    await Promise.all([loadOverview(), loadTrend(), loadModels(), loadLog(), loadFilters()]);
    renderUsage();
  } catch (e) {
    toast("采集失败: " + e.message, "err");
  } finally {
    usageState.collecting = false;
    if (btn) { btn.disabled = false; btn.textContent = "采集"; }
  }
}

function toast(msg) {
  // 复用 app.js 的 toast（如果存在），否则简单 console
  if (typeof window.toast === "function") window.toast(msg);
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
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
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
  v.innerHTML = `<div class="usage-loading">加载中…</div>`;
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
    (d) => `<option value="${d}" ${d === usageState.days ? "selected" : ""}>最近 ${d} 天</option>`
  ).join("");
  const srcOpts = ['<option value="">全部数据源</option>']
    .concat((usageState.filters.sources || []).map(
      (s) => `<option value="${esc(s.source)}" ${s.source === usageState.source ? "selected" : ""}>${esc(sourceLabel(s.source))} (${s.records})</option>`
    )).join("");
  const modelOpts = ['<option value="">全部模型</option>']
    .concat((usageState.filters.models || []).slice(0, 50).map(
      (m) => `<option value="${esc(m)}" ${m === usageState.model ? "selected" : ""}>${esc(m)}</option>`
    )).join("");
  return `
    <div class="usage-controls">
      <label>时间 <select id="usageDays">${daysOpts}</select></label>
      <label>数据源 <select id="usageSource">${srcOpts}</select></label>
      <label>模型 <select id="usageModel">${modelOpts}</select></label>
      <button class="btn btn-primary usage-collect-btn" id="usageCollectBtn">采集</button>
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
        <div class="usage-kpi-label">总 Token</div>
        <div class="usage-kpi-value">${fmtTokens(total)}</div>
        <div class="usage-kpi-sub">${o.requests || 0} 次请求 · ${o.sessions || 0} 个会话</div>
      </div>
      <div class="usage-kpi">
        <div class="usage-kpi-label">缓存命中率</div>
        <div class="usage-kpi-value ${o.cache_hit_rate >= 0.5 ? "kpi-good" : ""}">${fmtPct(o.cache_hit_rate)}</div>
        <div class="usage-kpi-sub">缓存读 / (输入+缓存写+缓存读)</div>
      </div>
      <div class="usage-kpi">
        <div class="usage-kpi-label">估算成本</div>
        <div class="usage-kpi-value">${o.has_cost ? fmtCost(o.cost) : "—"}</div>
        <div class="usage-kpi-sub">${o.has_cost ? "按单价表估算" : "未配置单价"}</div>
      </div>
      <div class="usage-token-bars">
        ${bar("输入", inp, "var(--accent)")}
        ${bar("输出", outp, "#10b981")}
        ${bar("缓存读", cr, "#8b5cf6")}
        ${bar("缓存写", cc, "#f59e0b")}
      </div>
    </div>`;
}

// ── 趋势图（纯 SVG，按天 4 桶折线，参考 VaultOne Usage Trend Chart）──
function renderTrendChart() {
  const data = usageState.trend || [];
  if (!data.length) return `<div class="usage-section"><h3>趋势</h3><div class="usage-empty">暂无数据</div></div>`;
  const W = 760, H = 200, PAD = { l: 50, r: 16, t: 16, b: 28 };
  const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const maxV = Math.max(1, ...data.flatMap((d) => [d.input, d.output, d.cache_creation, d.cache_read]));
  const x = (i) => PAD.l + (data.length <= 1 ? iw / 2 : (i / (data.length - 1)) * iw);
  const y = (v) => PAD.t + ih - (v / maxV) * ih;

  const series = [
    { key: "input", color: "var(--accent)", label: "输入" },
    { key: "output", color: "#10b981", label: "输出" },
    { key: "cache_read", color: "#8b5cf6", label: "缓存读" },
    { key: "cache_creation", color: "#f59e0b", label: "缓存写" },
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
      <h3>Token 趋势 <span class="usage-legend">${legend}</span></h3>
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
  if (!data.length) return `<div class="usage-section"><h3>模型分布</h3><div class="usage-empty">暂无数据</div></div>`;
  const metricLabel = usageState.metric === "cost" ? "成本" : "Token";
  const total = data.reduce((a, m) => a + (usageState.metric === "cost" ? m.cost : m.tokens), 0);
  const maxVal = Math.max(1, ...data.map((m) => (usageState.metric === "cost" ? m.cost : m.tokens)));
  const rows = data.map((m) => {
    const val = usageState.metric === "cost" ? m.cost : m.tokens;
    const pct = total > 0 ? (val / total * 100) : 0;
    const valStr = usageState.metric === "cost" ? fmtCost(m.cost) : fmtTokens(m.tokens);
    return `
      <div class="usage-model-row ${m.model === usageState.model ? "active" : ""}" data-model="${esc(m.model)}">
        <span class="usage-model-name">${esc(m.model)}</span>
        <div class="usage-model-bar"><div style="width:${(val / maxVal * 100)}%"></div></div>
        <span class="usage-model-val">${valStr}</span>
        <span class="usage-model-pct">${pct.toFixed(1)}%</span>
      </div>`;
  }).join("");
  return `
    <div class="usage-section">
      <h3>模型分布
        <span class="usage-metric-toggle">
          <button class="${usageState.metric === "tokens" ? "active" : ""}" data-metric="tokens">Token</button>
          <button class="${usageState.metric === "cost" ? "active" : ""}" data-metric="cost">成本</button>
        </span>
      </h3>
      <div class="usage-model-list">${rows}</div>
      <div class="usage-hint">点击模型名可按该模型筛选（再点取消）· 当前按 ${metricLabel} 排序</div>
    </div>`;
}

// ── 逐请求日志表 ──
function renderRequestLog() {
  const rows = (usageState.log && usageState.log.rows) || [];
  if (!rows.length) return `<div class="usage-section"><h3>请求日志</h3><div class="usage-empty">暂无数据</div></div>`;
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
      <h3>请求日志 <span class="usage-hint">最近 ${rows.length} 条（按时间倒序）</span></h3>
      <div class="usage-table-wrap">
        <table class="usage-table">
          <thead><tr>
            <th>时间</th><th>来源</th><th>模型</th>
            <th>输入</th><th>输出</th><th>缓存写</th><th>缓存读</th>
            <th>合计</th><th>成本</th><th>停止原因</th>
          </tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>
    </div>`;
}

function renderPricingNote() {
  return `
    <div class="usage-section usage-pricing-section">
      <h3>单价与成本</h3>
      <div class="usage-pricing-actions">
        <button class="btn btn-ghost" id="usageLitellmBtn">从 LiteLLM 更新单价</button>
        <button class="btn btn-ghost" id="usageRebillBtn">补算 0 成本记录</button>
        <button class="btn btn-ghost" id="usagePricingBtn">查看 / 编辑单价表</button>
        <span class="usage-hint">成本为按单价表的估算值，非真实账单</span>
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

  // 模型分布：点击行按模型筛选
  document.querySelectorAll(".usage-model-row").forEach((row) => {
    row.addEventListener("click", () => {
      const m = row.dataset.model;
      if (m && m.startsWith("其他")) return;
      usageState.model = (usageState.model === m) ? "" : m;
      reloadAll();
    });
  });
  // token / cost 切换
  document.querySelectorAll(".usage-metric-toggle button").forEach((btn) => {
    btn.addEventListener("click", () => {
      usageState.metric = btn.dataset.metric;
      loadModels().then(renderUsage);
    });
  });
  // 定价操作
  const litellm = $("#usageLitellmBtn");
  if (litellm) litellm.addEventListener("click", updateLitellm);
  const rebill = $("#usageRebillBtn");
  if (rebill) rebill.addEventListener("click", doRebill);
  const pricing = $("#usagePricingBtn");
  if (pricing) pricing.addEventListener("click", togglePricingPanel);
}

async function reloadAll() {
  await Promise.all([loadOverview(), loadTrend(), loadModels(), loadLog()]);
  renderUsage();
}

async function updateLitellm() {
  const btn = $("#usageLitellmBtn");
  if (btn) { btn.disabled = true; btn.textContent = "更新中…"; }
  try {
    const res = await fetch("/api/usage/pricing/litellm", { method: "POST" });
    const data = await res.json();
    if (data.error) toast("更新失败: " + data.error);
    else {
      toast(`已更新 ${data.updated} 个模型单价`);
      await doRebill(true); // 用新单价补算
    }
  } catch (e) { toast("更新失败: " + e.message); }
  finally { if (btn) { btn.disabled = false; btn.textContent = "从 LiteLLM 更新单价"; } }
}

async function doRebill(silent) {
  const btn = $("#usageRebillBtn");
  if (btn) { btn.disabled = true; btn.textContent = "补算中…"; }
  try {
    const res = await fetch("/api/usage/pricing/rebill", { method: "POST" });
    const data = await res.json();
    if (!silent) toast(`补算 ${data.recounted} 条（仍有 ${data.still_zero} 条无价）`);
    await reloadAll();
  } catch (e) { if (!silent) toast("补算失败: " + e.message); }
  finally { if (btn) { btn.disabled = false; btn.textContent = "补算 0 成本记录"; } }
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
  panel.innerHTML = `<div class="usage-loading">加载单价表…</div>`;
  try {
    const res = await fetch("/api/usage/pricing");
    const data = await res.json();
    renderPricingTable(panel, data.entries || []);
  } catch (e) {
    panel.innerHTML = `<div class="usage-empty">加载失败: ${esc(e.message)}</div>`;
  }
}

function renderPricingTable(container, entries) {
  if (!entries.length) {
    container.innerHTML = `<div class="usage-empty">单价表为空</div>`;
    return;
  }
  const rows = entries.map((e) => `
    <tr data-key="${esc(e.model_key)}">
      <td class="model">${esc(e.model_key)}${e.is_builtin ? '<span class="pricing-tag">内置</span>' : ""}</td>
      <td class="num">${e.input_per_million}</td>
      <td class="num">${e.output_per_million}</td>
      <td class="num">${e.cache_read_per_million}</td>
      <td class="num">${e.cache_creation_per_million}</td>
      <td>${e.is_builtin ? "" : `<button class="btn btn-ghost btn-sm" data-del="${esc(e.model_key)}">删</button>`}</td>
    </tr>`).join("");
  container.innerHTML = `
    <div class="usage-table-wrap">
      <table class="usage-table pricing-table">
        <thead><tr>
          <th>模型</th><th>输入 $/1M</th><th>输出 $/1M</th>
          <th>缓存读 $/1M</th><th>缓存写 $/1M</th><th></th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <details class="pricing-add">
      <summary>添加 / 覆盖模型单价</summary>
      <div class="pricing-add-form">
        <input id="pk" placeholder="模型 key（如 claude-sonnet-5）">
        <input id="pin" type="number" step="0.01" placeholder="输入 $/1M">
        <input id="pout" type="number" step="0.01" placeholder="输出 $/1M">
        <input id="pcr" type="number" step="0.01" placeholder="缓存读 $/1M">
        <input id="pcc" type="number" step="0.01" placeholder="缓存写 $/1M">
        <button class="btn btn-primary btn-sm" id="pSave">保存</button>
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
    if (!payload.model_key) { toast("请填模型 key"); return; }
    const res = await fetch("/api/usage/pricing", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (res.ok) {
      toast("已保存");
      await doRebill(true);
      togglePricingPanel(); togglePricingPanel();
    } else { toast("保存失败"); }
  });
}
