/* ── QuotaX 纯函数集合 ───────────────────────────────────────
   这里只放不依赖 DOM / localStorage / fetch 的纯函数：给什么输入就产出什么
   输出，没有副作用。目的是能被 node:test 直接 import 测试（不用起浏览器、
   不用 mock 任何全局对象），同时被 app.js 在真实渲染路径里复用，保证测试
   验证的就是线上实际跑的那份逻辑，而不是一份平行维护的"测试专用"实现。 */

/* 三个标准周期档位的定义（5小时 / 周 / 月）。normalizeThreeWindows 用它给
   每张卡补齐固定的三个槽位，保证卡片高度对齐、让用户看到"这个套餐没有月额度"
   这个产品意图本身是对的，之前的 bug 出在补齐的同时把补不齐三档的真实数据
   也一起丢了（见下面 fillGroup 的注释）。 */
const TIER_DEFS = [
  { key: "five_hour", label: "每 5 小时" },
  { key: "weekly", label: "每周额度" },
  { key: "monthly", label: "每月额度" },
];

/* 补齐一组窗口的三个标准档位，缺的补 max_label:"未提供" 占位；三档消费不掉的
   窗口（重复 key 的第二条及以后、custom/credits/balance 等非标准 key）按原始
   顺序追加在三档后面——"占位补齐"只解决"卡片高度对齐"，绝不能顺手把这三个
   标准 key 之外、或同 key 的第二条真实数据吞掉。
   用 Set 记录"已消费"的窗口对象引用而不是 key 去重：key 是允许重复的（例如
   Claude 订阅同时有两条 weekly：每周 Opus 额度 + 每周 Sonnet 额度），如果按
   key 判断"是否已经出现过"，第二条 weekly 会被误判成"重复"而漏加；按引用
   去重则只标记"真的被三档占用掉的那个对象"，其余原样保留。 */
function fillGroup(groupWindows, prefix = "") {
  const consumed = new Set();
  const tiers = TIER_DEFS.map((def) => {
    const fullKey = prefix ? `${prefix}${def.key}` : def.key;
    // 双条件匹配：后端拆卡后 windows 里的 key 带前缀（如 agent_five_hour），
    // 但也兼容不带前缀的情况，两边都能命中同一份 TIER_DEFS 定义。
    const existing = groupWindows.find(
      (w) => !consumed.has(w) && (w.key === fullKey || w.key === def.key)
    );
    if (existing) {
      consumed.add(existing);
      return existing;
    }
    return {
      key: fullKey,
      label: prefix ? `${prefix === "agent_" ? "Agent " : "Coding "}${def.label}` : def.label,
      used_percent: null,
      remaining_percent: null,
      max_label: "未提供",
    };
  });

  // 三个标准档位一个都没命中真实窗口：说明这组套餐根本不是按"5小时/周/月"分档的
  // （比如 Gemini 按模型分、OpenRouter/Copilot/new-api 中转站只有一条"已用比例"）。
  // 这种情况下"未提供"占位不是"缺数据"的信号，而是"这个概念对这个套餐不适用"——
  // 三条空占位顶在最上面既是噪音，还会把仅有的真实数据往下挤，比"数据丢了"好不了
  // 多少。只有当至少命中一个标准档位时，才说明这套餐确实是按三档分的（只是没订
  // 某一档），这时占位才有信息量，才值得补——原样返回，不做任何改写。
  if (consumed.size === 0) {
    return groupWindows;
  }

  const extra = groupWindows.filter((w) => !consumed.has(w));
  return [...tiers, ...extra];
}

/* 统一补齐标准的周期窗口（5小时 / 周 / 月）——但前提是这组窗口里至少有一个真的
   命中了这三档之一，否则说明这个套餐本来就不按这三档分（Gemini/Copilot/
   OpenRouter/new-api 这类：按模型或"已用比例"计费，压根没有"月额度"这个概念），
   此时不补占位、原样展示这组自己的窗口，避免把"概念不适用"误导成"缺数据"。
   如果同一个卡片下包含多个子 Plan（如火山 Agent Plan + Coding Plan），分别按组
   独立判断、按组补齐 3 个窗口——两组是否补占位互不影响，保证卡片高度一致的同时，
   所有存在的 Plan 均全量展示。
   核心约束（这也是之前 P0 bug 的修复点）：这个函数只负责"补三档占位"，不负责
   "筛选/裁剪"——任何传进来的窗口对象，最终必须在返回数组里原样出现恰好一次
   （要么被三档之一直接引用，要么被追加在后面，要么在整组都不补占位时原样保留），
   不允许凭空消失。 */
export function normalizeThreeWindows(windows) {
  const agentWindows = windows.filter((w) => (w.key || "").startsWith("agent_"));
  const codingWindows = windows.filter((w) => (w.key || "").startsWith("coding_"));

  if (agentWindows.length || codingWindows.length) {
    const res = [];
    if (agentWindows.length) res.push(...fillGroup(agentWindows, "agent_"));
    if (codingWindows.length) res.push(...fillGroup(codingWindows, "coding_"));
    // 分组场景下理论上不会出现既非 agent_ 也非 coding_ 前缀的窗口（火山后端
    // 拆卡时已经按前缀分好组），但防御性地按原顺序追加，避免以后接入新的分组
    // 渠道时又重演一次"函数只认识特定 key、其它全部静默丢弃"的老 bug。
    const grouped = new Set([...agentWindows, ...codingWindows]);
    const rest = windows.filter((w) => !grouped.has(w));
    res.push(...rest);
    return res;
  }

  return fillGroup(windows);
}

/* 火山渠道在后端 /api/quotas 里被拆成 <配置id>_agent / <配置id>_coding 两张卡
   展示（前端拿到的卡片 id 带后缀），但配置本身、低余额阈值等都按不带后缀的
   原始 config id 存取。凡是要拿"卡片 id"去查"配置 id 维度"数据的地方（编辑
   弹窗回填、低余额阈值查找）都必须先归一化，否则两边 key 永远对不上、功能
   悄悄失效却不报错。这是后端 app/main.py 里 _canonical_channel_id 的前端
   镜像——前后端目前没有共享代码的机制，只能两边各维护一份同样的逻辑。 */
export function canonicalChannelId(id) {
  return String(id ?? "").replace(/_(agent|coding)$/, "");
}

/* 两个百分比字段都缺失：这个条目只是文本标签（如"充值余额""订阅计划"），没有百分比概念 */
export function noPercentData(w) {
  const noUsed = w.used_percent === null || w.used_percent === undefined;
  const noRemaining = w.remaining_percent === null || w.remaining_percent === undefined;
  return noUsed && noRemaining;
}

/* 判断某渠道是否触发低余额阈值告警（remaining_percent < 阈值）。
   只对有百分比窗口的 ok 渠道生效；info/error/disabled 不参与。
   thresholds 由调用方传入（而不是这里自己读 localStorage）：一是让判断逻辑
   保持纯函数、能在没有 localStorage 的环境（node:test）里直接测试；二是查表
   前必须先用 canonicalChannelId 归一 id——火山子卡片 id 带 _agent/_coding
   后缀，阈值却是按配置 id 存的，不归一就永远查不到对应阈值，告警也就永远
   不会触发（这正是之前的 P1 bug）。 */
export function channelBreachesThreshold(q, thresholds) {
  if (q.status !== "ok" || !q.windows) return false;
  const threshold = thresholds[canonicalChannelId(q.id)];
  if (threshold === undefined) return false;
  return q.windows.some(
    (w) => w.remaining_percent !== null && w.remaining_percent !== undefined && w.remaining_percent < threshold
  );
}

/* 计算"重置还有多久"的展示文案。now 默认取当前时间；测试里可以传一个固定值，
   避免用例因为真实时钟在整分/整时边界上流逝而偶发抖动——生产环境的调用方
   （renderBar/renderBarFlat）都不传第二个参数，行为跟之前完全一致。 */
export function fmtReset(ms, now = Date.now()) {
  const diff = ms - now;
  if (diff <= 0) return "已重置";
  if (diff < 3600_000) return `${Math.ceil(diff / 60_000)} 分钟后`;
  if (diff < 86400_000) return `${Math.ceil(diff / 3600_000)} 小时后`;
  return `${Math.ceil(diff / 86400_000)} 天后`;
}
