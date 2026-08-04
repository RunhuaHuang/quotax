/* ── 前端纯函数测试 ───────────────────────────────────────────
   只用 Node 内置的 node:test + node:assert，不引入任何新依赖，`node --test
   tests/frontend/` 直接跑。这里专门覆盖 static/view-utils.js 里的纯函数——
   都是从 app.js 抽出来的、之前只能靠人肉盯着代码审查的逻辑，出过真实的 P0/P1
   bug（normalizeThreeWindows 丢数据、火山卡阈值查不到），所以优先把回归场景
   钉死，而不是追求覆盖率。 */

import { describe, test } from "node:test";
import assert from "node:assert/strict";
import {
  normalizeThreeWindows,
  canonicalChannelId,
  channelBreachesThreshold,
  noPercentData,
  fmtReset,
} from "../../static/view-utils.js";

describe("normalizeThreeWindows", () => {
  test("Gemini 订阅：0 个标准档位命中时不补占位，只原样展示 3 个 custom 窗口（回归：之前先是数据全丢，修复后又变成 3 条噪音占位顶在最上面）", () => {
    const windows = [
      { key: "custom", label: "Gemini 2.5 Pro", used_percent: 82, remaining_percent: 18, max_label: "100 次/天" },
      { key: "custom", label: "Gemini 2.5 Flash", used_percent: 95, remaining_percent: 5, max_label: "250 次/天" },
      { key: "custom", label: "Gemini 2.5 Flash-Lite", used_percent: 100, remaining_percent: 0, max_label: "1000 次/天" },
    ];
    const result = normalizeThreeWindows(windows);
    // Gemini 按模型分配额，压根没有 five_hour/weekly/monthly 这个概念——不该出现任何
    // "未提供"占位（那会暗示"本该有数据但没查到"，实际是"这个概念不适用"）。
    assert.equal(result.length, 3);
    for (let i = 0; i < windows.length; i++) {
      assert.strictEqual(result[i], windows[i]); // 原对象、原顺序，一条不丢也不改写
    }
  });

  test("OpenRouter / new-api 中转站：单条「已用比例」custom 窗口，0 标准档位命中，不补占位", () => {
    const windows = [{ key: "custom", label: "已用比例", used_percent: 63, remaining_percent: 37, max_label: "" }];
    const result = normalizeThreeWindows(windows);
    assert.equal(result.length, 1);
    assert.strictEqual(result[0], windows[0]);
  });

  test("GitHub Copilot：Chat 对话 / 高级模型请求两条 custom 窗口，0 标准档位命中，不补占位", () => {
    const windows = [
      { key: "custom", label: "Chat 对话", used_percent: 30, remaining_percent: 70, max_label: "" },
      { key: "custom", label: "高级模型请求", used_percent: 88, remaining_percent: 12, max_label: "" },
    ];
    const result = normalizeThreeWindows(windows);
    assert.equal(result.length, 2);
    assert.strictEqual(result[0], windows[0]);
    assert.strictEqual(result[1], windows[1]);
  });

  test("Claude 订阅：five_hour + 两条 weekly（Opus/Sonnet）都保留，不因同 key 互相顶替", () => {
    const windows = [
      { key: "five_hour", label: "每 5 小时", used_percent: 40, remaining_percent: 60, max_label: "" },
      { key: "weekly", label: "每周 Opus 额度", used_percent: 70, remaining_percent: 30, max_label: "" },
      { key: "weekly", label: "每周 Sonnet 额度", used_percent: 20, remaining_percent: 80, max_label: "" },
    ];
    const result = normalizeThreeWindows(windows);
    assert.equal(result.length, 4);
    assert.strictEqual(result[0], windows[0]); // five_hour 原样
    assert.strictEqual(result[1], windows[1]); // 第一条 weekly（Opus）占据标准槽位
    assert.equal(result[2].key, "monthly");
    assert.equal(result[2].max_label, "未提供"); // 月额度确实没有，补占位
    assert.strictEqual(result[3], windows[2]); // 第二条 weekly（Sonnet）之前被 find() 吃掉，现在追加保留
  });

  test("five_hour + weekly 正常入三档时，额外的 custom 窗口追加在后面不丢", () => {
    const windows = [
      { key: "five_hour", label: "每 5 小时", remaining_percent: 50, max_label: "" },
      { key: "weekly", label: "每周额度", remaining_percent: 66, max_label: "" },
      { key: "custom", label: "其它额度", remaining_percent: 10, max_label: "" },
    ];
    const result = normalizeThreeWindows(windows);
    assert.equal(result.length, 4);
    assert.strictEqual(result[0], windows[0]);
    assert.strictEqual(result[1], windows[1]);
    assert.equal(result[2].key, "monthly");
    assert.equal(result[2].max_label, "未提供");
    assert.strictEqual(result[3], windows[2]);
  });

  test("只有 five_hour 时补齐 weekly/monthly 占位（非回归：保护原有产品意图不被改坏）", () => {
    const windows = [{ key: "five_hour", label: "每 5 小时", remaining_percent: 88, max_label: "" }];
    const result = normalizeThreeWindows(windows);
    assert.equal(result.length, 3);
    assert.strictEqual(result[0], windows[0]);
    assert.equal(result[1].key, "weekly");
    assert.equal(result[1].max_label, "未提供");
    assert.equal(result[2].key, "monthly");
    assert.equal(result[2].max_label, "未提供");
  });

  test("火山 agent_/coding_ 分组：每组各自补齐三档，组内额外窗口不丢", () => {
    const windows = [
      { key: "agent_five_hour", label: "Agent 每 5 小时", remaining_percent: 40, max_label: "" },
      { key: "agent_weekly", label: "Agent 每周额度", remaining_percent: 55, max_label: "" },
      { key: "agent_custom_extra", label: "Agent 额外明细", remaining_percent: 12, max_label: "" },
      { key: "coding_five_hour", label: "Coding 每 5 小时", remaining_percent: 70, max_label: "" },
      { key: "coding_monthly", label: "Coding 每月额度", remaining_percent: 33, max_label: "" },
    ];
    const result = normalizeThreeWindows(windows);
    // agent 组：five_hour(真) + weekly(真) + monthly(占位) + custom_extra(追加) = 4 条
    // coding 组：five_hour(真) + weekly(占位) + monthly(真) = 3 条
    assert.equal(result.length, 7);
    assert.strictEqual(result[0], windows[0]); // agent_five_hour
    assert.strictEqual(result[1], windows[1]); // agent_weekly
    assert.equal(result[2].key, "agent_monthly");
    assert.equal(result[2].max_label, "未提供");
    assert.strictEqual(result[3], windows[2]); // agent 组内额外窗口，不属于三档，追加不丢
    assert.strictEqual(result[4], windows[3]); // coding_five_hour
    assert.equal(result[5].key, "coding_weekly");
    assert.equal(result[5].max_label, "未提供");
    assert.strictEqual(result[6], windows[4]); // coding_monthly
  });

  test("分组场景按组独立判断补占位：一组 0 标准档位命中就不补，另一组照常补，互不影响", () => {
    const windows = [
      { key: "agent_custom_extra", label: "Agent 额外明细", remaining_percent: 12, max_label: "" }, // agent 组无任何标准档位
      { key: "coding_five_hour", label: "Coding 每 5 小时", remaining_percent: 70, max_label: "" }, // coding 组命中一个标准档位
    ];
    const result = normalizeThreeWindows(windows);
    // agent 组：0 命中 → 原样返回，只有 1 条，不补 3 档占位
    // coding 组：命中 five_hour → 正常补齐 weekly/monthly 占位，3 条
    assert.equal(result.length, 4);
    assert.strictEqual(result[0], windows[0]); // agent_custom_extra 原样，前面没有被硬塞 3 条占位
    assert.strictEqual(result[1], windows[1]); // coding_five_hour 真实数据
    assert.equal(result[2].key, "coding_weekly");
    assert.equal(result[2].max_label, "未提供");
    assert.equal(result[3].key, "coding_monthly");
    assert.equal(result[3].max_label, "未提供");
  });
});

describe("canonicalChannelId", () => {
  test("火山子卡 id 归一到配置 id，普通 id 原样返回", () => {
    assert.equal(canonicalChannelId("ch_a_agent"), "ch_a");
    assert.equal(canonicalChannelId("ch_a_coding"), "ch_a");
    assert.equal(canonicalChannelId("ch_abc123"), "ch_abc123");
  });
});

describe("channelBreachesThreshold", () => {
  test("火山后缀卡片能命中按配置 id 存的阈值（回归 P1：之前两边 key 对不上，永远不告警）", () => {
    const thresholds = { ch_volc123: 20 }; // 阈值按配置 id（不带 _agent/_coding 后缀）存
    const lowChannel = {
      id: "ch_volc123_agent",
      status: "ok",
      windows: [{ key: "agent_five_hour", remaining_percent: 10, used_percent: 90 }],
    };
    assert.equal(channelBreachesThreshold(lowChannel, thresholds), true);

    const okChannel = {
      id: "ch_volc123_coding",
      status: "ok",
      windows: [{ key: "coding_five_hour", remaining_percent: 50, used_percent: 50 }],
    };
    // 阈值查得到（归一后同一个配置 id），只是 50% 没有低于 20% 阈值，不应告警
    assert.equal(channelBreachesThreshold(okChannel, thresholds), false);
  });

  test("未设置阈值 / 非 ok 状态 / 无窗口数据都不告警", () => {
    assert.equal(channelBreachesThreshold({ id: "ch_x", status: "ok", windows: [{ remaining_percent: 1 }] }, {}), false);
    assert.equal(
      channelBreachesThreshold({ id: "ch_x", status: "error", windows: [{ remaining_percent: 1 }] }, { ch_x: 50 }),
      false
    );
    assert.equal(channelBreachesThreshold({ id: "ch_x", status: "ok", windows: null }, { ch_x: 50 }), false);
  });
});

describe("noPercentData", () => {
  test("used_percent 和 remaining_percent 都缺失才判定为无百分比数据", () => {
    assert.equal(noPercentData({ used_percent: null, remaining_percent: null }), true);
    assert.equal(noPercentData({}), true);
    assert.equal(noPercentData({ used_percent: 50, remaining_percent: null }), false);
    assert.equal(noPercentData({ used_percent: null, remaining_percent: 50 }), false);
  });
});

describe("fmtReset", () => {
  test("按不同时间跨度展示对应文案（传入固定 now，避免真实时钟流逝导致偶发抖动）", () => {
    const now = Date.parse("2026-08-04T00:00:00Z");
    assert.equal(fmtReset(now - 1000, now), "已重置");
    assert.equal(fmtReset(now + 30 * 60 * 1000, now), "30 分钟后");
    assert.equal(fmtReset(now + 5 * 3600 * 1000, now), "5 小时后");
    assert.equal(fmtReset(now + 3 * 86400 * 1000, now), "3 天后");
  });
});
