# QuotaX · 设计稿 (.design)

本目录是 Solo Design 项目规范下的设计稿归档，承载本次 WebUI 优化的设计意图、token 决策与静态渲染证据。

## 设计目标
- **专业、简洁、有质感**：参考 Google Material Design 3 (M3) 的 surface / color role / motion 体系，
  沿用 Google Blue + Cyan 品牌副色，搭配等宽数字与品类色块强调数据层级。
- **保留核心功能与数据结构**：所有 API 契约、`/static/{index.html, app.js}` 的数据 shape 与事件流不变。
- **不修改只读参考规范目录**：本次未引用任何外部规范包，未修改 `tests/`、`app/` 等代码。

## 设计决策（关键）

### 1 · Tokens（全部以 CSS 变量形式落地于 `static/styles.css`）
| 角色 | 取值 | 用途 |
| --- | --- | --- |
| `--primary` | `#1a73e8` (Google Blue 600) | 主 CTA / 选中态 / 进度 |
| `--secondary` | `#00acc1` (Cyan 600) | 渐变副色 / 摘要信息 |
| `--green / --amber / --red / --info` | M3 语义色 + `--{name}-bg / -on-bg` | 状态点 / 标签 / 错误 |
| `--bg / --surface / --surface-2..4 / --outline` | M3 surface tints | 卡片层、容器底色 |
| `--cat-{balance, coding_plan, subscription, local}` | 4 个品类主色 | 卡片顶条 / section 圆点 |
| `--font-display / --font-sans / --font-mono` | Google Sans · Roboto · Roboto Mono | 中英文双轨 fallback |
| `--fs-*` | M3 type scale | display/headline/title/body/label |
| `--sp-1..9` | 4pt 网格 | 间距统一规范 |
| `--r-{xs,sm,md,lg,xl,pill}` | M3 shapes | 圆角统一规范 |
| `--elev-1..4` | M3 tonal shadows | 顶栏 / 卡片 / 弹窗 |
| `--ease-{standard,emph,accel,decel}` + `--dur-{fast,base,slow}` | M3 motion | 动效统一规范 |

### 2 · 布局层级
- **顶栏 (sticky, blur backdrop)**：品牌区 + 摘要 chips + 4 个操作按钮（auto-refresh switch / 刷新 / 趋势 / 设置 / 配置渠道）。
- **主区**：时间戳状态条 → 按 `balance / coding_plan / subscription / local` 分组的卡片栅格。
- **卡片栅格**：`grid-template-columns: repeat(auto-fill, minmax(320px, 1fr))` —— 桌面 3 列、平板 2 列、手机单列。
- **底部信息条**：本地统计区块使用 surface 卡片包裹 stat grid + model 表格。

### 3 · 卡片信息架构
- **顶条**：`::before` 伪元素按品类色绘制 3px 高亮带。
- **头部**：品类 icon tile（渐变 + 阴影）+ 渠道名（display, 500）+ 计划/来源（dim 副标）+ 状态圆点 + 状态标签。
- **主体**：金额（大号 mono · tabular-nums）+ currency + 多窗口（≤4 用环，>4 切换到条形）。
- **底部**：来源信息 + 时间戳 + 局部刷新/启停快捷按钮（hover/focus 显现）。

### 4 · 状态反馈
- `ok / info / expired / error / not_found / disabled` 六态一一对应 `--green / --info / --red / --amber / --outline-var / --text-faint`。
- 状态圆点 + 状态标签 双层冗余：弱视用户也能仅凭文字辨识。
- `card-busy` 顶条流光 + 卡片半透明，避免与全屏 skeleton 混淆。
- `is-low-alert` 低额度告警：amber 边框 + 阴影 + 顶条 amber。
- Toast 使用 M3 snackbar 形态：深色背景（暗色模式与亮色模式均 #323232 系）、左侧 3px 彩条、滑入而非横推。

### 5 · 响应式
- `≥1024px`：桌面布局，3 列卡片。
- `≤1024px`：减内边距，最小列宽降到 280px。
- `≤720px`：单列、隐藏摘要 chips 与品牌副标、卡片更紧凑、按钮压缩字号。
- `≤420px`：按钮仅留图标、隐藏"设置"快捷按钮。

### 6 · 可访问性
- 所有图标 SVG 加 `aria-hidden="true"`。
- 模态关闭按钮加 `aria-label`。
- Toasts 容器加 `role="status" aria-live="polite"`。
- 刷新按钮在请求时设置/移除 `aria-busy`。
- 全局 `:focus-visible` 用 primary 色 2px 描边作为键盘焦点环。

## 静态渲染证据
`assets/` 下 8 张 Playwright 渲染产物，覆盖 light/dark × 桌面/移动 × 主面板/配置弹窗/趋势弹窗：
- `dashboard-light-desktop.png` — 浅色桌面 1440×900（含所有分类 + 本地统计）
- `dashboard-dark-desktop.png` — 深色桌面 1440×900
- `dashboard-light-mobile.png` — 浅色 iPhone 14 尺寸 390×844
- `dashboard-dark-mobile.png` — 深色移动端
- `config-modal-light.png` / `config-modal-dark.png` — 渠道配置弹窗
- `history-modal-light.png` / `history-modal-dark.png` — 历史趋势弹窗（含 sparkline）

## 修改文件
- `static/styles.css`（重写 tokens + 组件样式 + 响应式 + 主题）
- `static/index.html`（按钮 `<span class="label">` 包裹、aria 属性、description meta、toast region）
- `static/app.js`（`refreshQuotas` 设置 `aria-busy`）

未触碰 `app/`、`tests/`、`README.md`、`pyproject.toml`、`uv.lock` 与任何后端文件。