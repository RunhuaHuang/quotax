# QuotaX

> A local WebUI to view all your AI **credits · subscription usage · Coding Plan quotas** in one place.

English | [简体中文](./README.md)

Aggregate balances and quotas scattered across vendor websites, CLIs, and consoles onto a single local dashboard. Subscription channels auto-read your local CLI login state — no API keys needed.

![QuotaX dashboard (light)](docs/dashboard-main.png)

---

## Features

- **Multi-channel aggregation**: Balance (DeepSeek / Stepfun / SiliconFlow / OpenRouter / Novita / Kimi API / new-api·one-api proxies), Coding Plans (Kimi / Zhipu GLM individual+team / MiniMax / Volcengine Ark / ZenMux / Xiaomi MiMo), Subscription usage (Claude / Gemini / Grok / Codex / Copilot / OpenCode), Local stats (Claude Code / OpenCode transcripts).
- **Zero-key subscriptions + auto-detect**: Reads local CLI credentials (Claude / Gemini / Grok / Codex / Copilot) — **no refresh, no write**, sharing the same login state as your agent. On startup it auto-detects locally logged-in CLIs and creates channels for them — no manual setup needed.
- **Usage analytics dashboard**: A dedicated tab with SQLite-persisted incremental collection — four-bucket token overview, cache hit rate, daily trend curves, model distribution, per-request log, cost estimation (LiteLLM pricing + rebill).
- **Codex OAuth in-browser**: ChatGPT subscriptions support one-click "Login with ChatGPT" OAuth — automatically obtains credentials and creates the channel.
- **Per-channel cache + request coalescing**: 60s on success / 15s on failure; concurrent queries to the same channel hit the upstream only once.
- **Drag-to-reorder**: Cards can be dragged to customize order; persisted to browser `localStorage`.
- **History trends**: Each successful query records a data point; SVG line charts show balance / remaining-percent over time.
- **Low-balance alerts**: Set a remaining-percent threshold per channel; cards turn orange and the summary chip counts alerts when below.
- **Bilingual (zh / en)**: Toggle between Simplified Chinese and English with one click.
- **Dark / light theme**: Follow system or set manually.
- **CLI tool**: `quotaboard quota --brief` one-line summary, great for tmux statusbar / shell prompts.
- **Config import / export**: Full backup with secrets (machine migration) or redacted export (safe to share channel structure).

![QuotaX dashboard (dark)](docs/dashboard-dark.png)

## One-line install

Open a terminal and paste the command for your OS. The script downloads the source, prepares the Python runtime, installs all dependencies, starts the service, and opens your browser.

**macOS / Linux:**

```bash
curl -fsSL https://raw.githubusercontent.com/RunhuaHuang/quotax/main/install.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/RunhuaHuang/quotax/main/install.ps1 | iex
```

> - First install prepares the runtime: **prefers reusing any local Python 3.11+**; only installs Python 3.13 automatically if none is found. No need to pre-install Python. Please allow 1–2 minutes.
> - Installs to `~/QuotaX` (`%USERPROFILE%\QuotaX` on Windows); service listens on `127.0.0.1:8900`.
> - **Auto-detect on install**: locally logged-in CLIs (Claude / Codex / Gemini / Grok / Copilot) are recognized and channels created automatically — no manual setup.
> - **Network blocked / GitHub firewalled?** The script has built-in multi-mirror fallback (GitHub → ghfast.top → gh-proxy.com → moeyy); or pin a mirror manually:
>   ```bash
>   QUOTAX_MIRROR=https://ghfast.top bash -c "$(curl -fsSL https://raw.githubusercontent.com/RunhuaHuang/quotax/main/install.sh)"
>   ```
>   ```powershell
>   $env:QUOTAX_MIRROR='https://ghfast.top'; irm https://raw.githubusercontent.com/RunhuaHuang/quotax/main/install.ps1 | iex
>   ```
> - **Upgrade**: just re-run the same command; personal data (`config.json` / `usage.db` / `history/`) is preserved.

### Opening later

After the first install, open the app any time by typing in a terminal:

```
quotax
```

This starts the background service and opens the browser (or just opens the browser if already running). Other commands:

| Command | Action |
| --- | --- |
| `quotax` | Start service and open WebUI (or open browser if running) |
| `quotax stop` | Stop the background service |
| `quotax status` | Show running status |
| `quotax log [N]` | Show last N log lines (default 50, macOS/Linux only) |

## Supported channels

| Category | Channels | Auth |
| --- | --- | --- |
| Balance | DeepSeek / Stepfun / SiliconFlow / OpenRouter / Novita / Kimi API / new-api·one-api proxies | API Key |
| Coding Plan | Kimi For Coding / Zhipu GLM Coding (individual+team) / MiniMax Token Plan / Volcengine Ark Agent·Coding (AK/SK) / ZenMux / Xiaomi MiMo | API Key / AK·SK / Cookie |
| Subscription | Claude Pro·Max / Gemini AI Studio / Grok SuperGrok·X / ChatGPT Codex / GitHub Copilot / OpenCode Zen·Go | Claude/Gemini/Grok/Codex/Copilot **auto-read local CLI login**; OpenCode needs **Cookie + Workspace ID** |
| Local stats | Claude Code / OpenCode local token usage (+ OpenCode cost if available) | None (reads local files/DB) |

![Config modal](docs/config-modal.png)

### new-api / one-api proxies

Tries the native `/api/user/self` first; if the deployment requires a "system access token + `New-API-User` header" instead of a regular `sk-` key, you can optionally fill in a `user_id` in the channel config (maps to the `New-API-User` request header), or leave it blank to auto-fallback to the OpenAI-compatible `/v1/dashboard/billing/subscription` + `/v1/dashboard/billing/usage`.

### Volcengine Ark

One account can have **both Agent Plan and Coding Plan** — the two plans are queried separately and shown as **left/right tabs** (card top has "Agent Plan" / "Coding Plan" tabs; within each tab the 5-hour / weekly / monthly windows are laid out in a row; a plan that wasn't found hides its tab). Window keys carry the plan dimension (`agent_*`/`coding_*`); history charts also split by plan. A failure on one plan doesn't affect the other. Create Access Keys in the Volcengine console: https://console.volcengine.com/iam/keymanage

### Xiaomi MiMo

The usage endpoint (`platform.xiaomimimo.com/api/v1/tokenPlan/usage`) only accepts the **Cookie** from a logged-in Xiaomi account (not an API Key). Log into platform.xiaomimimo.com and copy the full Cookie from browser DevTools into the channel config's "Cookie" field.

### OpenCode (Zen/Go)

opencode.ai is an SSR page with no public JSON API; quota data (rolling / weekly / monthly used-percentage and reset countdowns) is embedded as a serialized React Server Component string in the HTML of `https://opencode.ai/workspace/<workspace-id>/go`. Login is required — copy two things from your browser into the channel config after logging into opencode.ai: ① the full **Cookie**; ② the **Workspace ID** from the URL bar (`wrk_xxx` format). The Cookie is stored only in local config.json (permissions 600), used for read-only queries. Reference: [Doueen/opencode-usage-extension](https://github.com/Doueen/opencode-usage-extension).

### ChatGPT (Codex) subscription

Three ways to obtain credentials:

1. **OAuth in-browser** (recommended) — click "Login with ChatGPT" when creating a Codex channel; the browser opens the OpenAI auth page, and after you log in and authorize, the PKCE flow completes automatically, returning a token, parsing the account_id, and creating the channel — no manual file handling. Repeat for multiple accounts.
2. **Upload auth.json** — if you've logged in with the Codex CLI locally, you can upload `~/.codex/auth.json` (one per channel for multi-account).
3. **Auto-read local CLI login** — leave everything blank; auto-reads `Codex Auth` from macOS Keychain or `~/.codex/auth.json`.

OAuth implementation references [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (client_id / endpoints / PKCE / id_token parsing match).

### Claude Code subscription: may only have local stats, no live usage

New Claude Code on macOS stores login state in the Keychain entry `Claude Code-credentials`, but **this entry may not contain a plaintext access token** — if `accessToken`/`refreshToken` are empty strings (only `subscriptionType` / `rateLimitTier` / `scopes` metadata), the account is logged in but no usable token is stored locally, so the official usage windows (`api.anthropic.com/api/oauth/usage`) can't be queried.

This is not "not logged in", so QuotaX **does not** prompt "please re-login". Instead it **auto-tries PTY probing**: launches the `claude` CLI in a pseudo-terminal (PTY) to run `/usage` — the CLI has its own Keychain access and can display the real usage panel in the terminal; QuotaX parses the terminal text to extract the 5-hour / weekly remaining percentages (reference: [CodexBar](https://github.com/steipete/CodexBar)'s `ClaudeStatusProbe`). If PTY probing is also unavailable (CLI not installed / not logged in), it falls back to `status: "info"` + local transcript stats.

> PTY probing requires the Claude CLI to be logged in locally (`claude` works).

## Usage analytics dashboard

![Usage dashboard](docs/usage-dashboard.png)

Switch via the top-bar "Quota / **Usage**" tab. The Usage tab is a standalone analytics dashboard backed by SQLite-persisted incremental collection — trend / model distribution / request log / cost estimation. Design reference: [Buktal/VaultOne](https://github.com/Buktal/VaultOne).

Unlike the quota page's local stats (real-time full aggregation, no persistence, just a current-window summary), this one **persists parsed results** to `usage.db` (same dir as config.json), enabling history trends, per-request detail, and incremental scanning (no full re-scan).

### Data sources

Reads only local AI CLI log files / databases — no network requests:

| Source | Path | Format | Notes |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/projects/*/*.jsonl` | JSONL | `type=assistant` lines' `message.usage`, deduped by `message.id` |
| Codex | `~/.codex/sessions/**/*.jsonl` + `archived_sessions/*.jsonl` | JSONL | `token_count` events; cumulative tokens need delta; input includes cache |
| Gemini CLI | `~/.gemini/tmp/<hash>/chats/session-*.json` | single JSON | `role=model` messages; input includes cache; output includes thoughts |
| Grok CLI | `~/.grok/sessions/<cwd>/<uuid>/updates.jsonl` | JSONL (JSON-RPC) | `turn_completed` event's `usage`; input includes cache |
| OpenCode | `~/.local/share/opencode/opencode.db` | SQLite | new version reads session aggregate columns; old version parses message.data JSON |

**Cache-inclusive normalization**: Codex / Gemini / Grok input fields all include cache-hit portions; parsing subtracts `cache_read` to get "fresh input", aligning with Claude Code's semantics.

**Incremental collection**: JSONL sources record a `(mtime, line_offset)` cursor per file and read only new lines past the cursor; truncated lines (concurrent writes) keep the old cursor for next read. The SQLite source (opencode) records a watermark. Re-collection is idempotent (`INSERT OR IGNORE` dedup by primary key), producing no duplicate records.

### Frontend dashboard

First load on entering the "Usage" tab includes:

- **Overview KPIs**: total tokens (four-bucket sum), cache hit rate (`cache_read / (input + cache_creation + cache_read)`, output excluded from denominator), estimated cost, request / session counts; four-bucket proportion bars.
- **Token trend curve**: pure SVG line chart, 4 independent lines (input / output / cache_read / cache_creation), aggregated by day, zero-filled for continuity.
- **Model distribution**: Top 8 + "other" summary, supports **token / cost** metric toggle; click a model name to cross-filter the whole dashboard.
- **Request log table**: per-request detail (time / source / model / four-bucket tokens / total / cost / stop reason), newest first; stop reasons are semantically colored (`end_turn` green / `tool_use` blue / `max_tokens` yellow / `error` red).
- **Filters**: time range (7/14/30/90 days), source, model — three-dimensional, linked to all sections.

### Cost estimation & pricing table

Costs are **estimated from a pricing table**, not real bills. Pricing sources:

1. **Built-in prices**: the project ships common-model prices (updated per release).
2. **LiteLLM fetch**: click "Update pricing from LiteLLM" to pull the latest from [LiteLLM model_prices](https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json), overriding built-ins.
3. **Manual edit**: add / override any model's input / output / cache-read / cache-write prices in the pricing panel.

All persisted prices live only in the local `usage.db` `model_pricing` table, merged with built-ins for cost calculation. After changing prices, click "Recalc zero-cost records" to reprice historical data.

## Card reordering

Dashboard cards are grouped by **category** (Coding Plan → Subscription → Balance → Local) by default. Within each category you can **drag the handle (⠿) at the card's bottom-right to reorder freely**:

- Drag a card onto another in the same category to swap them;
- Order persists to browser `localStorage` (`quotaboard_prefs.card_order`), surviving refresh / reopen;
- Newly added channels append to the end of their category;
- Cross-category drags are blocked (category is decided by the backend).

## History trends

![History modal](docs/history-modal.png)

Click the top-bar "Trend" button. Each successful quota query records a data point (history stored as JSONL under the `history/` subdirectory, same dir as config.json). Filter by channel and time range (7/14/30/90/180 days); SVG line charts show balance or remaining-percent over time. Volcengine Agent / Coding plans split into separate lines.

## Low-balance alerts

![Settings modal](docs/settings-modal.png)

Set a "remaining-percent threshold" per channel in the settings modal. When below: card border turns orange, status dot turns red; the top-bar summary chip shows a "Low" count. Thresholds are browser-local only and don't affect backend queries.

## Config import / export

At the bottom of the config modal:

- **Export (with secrets)**: exports the full `config.json` (plaintext API Keys) for personal backup / migration. Keep it safe.
- **Export (redacted)**: exports the channel structure without secrets (type / name / base_url etc.), safe to share.
- **Import**: choose **merge** (append to existing, same-id overwrites — safer, recommended) or **replace** (clears all existing channels first).

## Non-intrusive login design

All subscription channels (Claude / Gemini / Grok / Codex / Copilot) only read local CLI credential files / Keychain — **never refresh tokens, never write any credential copy**. Your local agent login state is completely undisturbed; QuotaX is a read-only observer. When a token expires, the card just shows "Expired" — you need to re-login in the corresponding CLI; QuotaX won't do OAuth refresh for you.

## CLI integration

Besides the WebUI, a CLI tool (`quotaboard`, via `uv run quotaboard`) is available for one-shot quota queries in terminals, tmux, shell prompts, or scripts:

```bash
uv run quotaboard quota                  # query all channels' quota summary (columnar)
uv run quotaboard quota --json           # JSON output (for scripting)
uv run quotaboard quota --brief          # compact mode (for shell prompts)
uv run quotaboard quota --ids ch_a,ch_b  # query specific channels only
uv run quotaboard channels               # list configured channels (masked secrets)
uv run quotaboard cost --days 7          # local token usage stats
```

Exit codes: `0` all OK; `1` has error/expired channels; `2` config corrupted or usage error — handy for health checks in scripts.

## Caching

To avoid bothering upstream APIs too often, each channel's query result has an independent in-process cache: **60s on success, 15s on failure**. Concurrent queries to the same channel within the cache TTL coalesce into one upstream request (dedup / in-flight reuse). Click the top-bar "Refresh" button to force-bypass the cache for a real query.

## Security

- The service **listens only on `127.0.0.1`** — never exposed externally.
- All credentials (API Key / AK·SK / Cookie) are stored only in local `config.json` (permissions `600`), never sent to any third party.
- Subscription channels only read local CLI credentials — no refresh, no write.
- Redirects are not followed by default (`follow_redirects: false`), preventing malicious base_url 3xx redirects from leaking the Authorization header.
- DNS-rebinding protection: request Host header is validated against a whitelist, preventing malicious web pages from cross-origin reading of local endpoints.

## Development

```bash
git clone https://github.com/RunhuaHuang/quotax.git
cd quotax
uv sync                      # install deps (reuses local Python 3.11+)
uv run pytest                # run tests (249 cases)
uv run uvicorn app.main:app --port 8900   # start dev server
```

For testing, point to a temp config to avoid touching the real `config.json`:

```bash
QUOTABOARD_CONFIG=/tmp/quotax-test/config.json uv run uvicorn app.main:app --port 8931
```

The frontend is pure static files (no build step) — edit `static/` JS/CSS and refresh.

## Tech stack

- **Backend**: Python 3.11+ · FastAPI · httpx · no external DB (config.json + SQLite + JSONL)
- **Frontend**: vanilla ES Modules · zero build / zero deps · self-hosted fonts (DM Sans + JetBrains Mono)
- **Runtime**: uv (manages Python versions and virtualenvs automatically)

## References

- [CodexBar](https://github.com/steipete/CodexBar) — Claude PTY usage probing, Codex credential parsing
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) — Codex OAuth PKCE flow
- [Buktal/VaultOne](https://github.com/Buktal/VaultOne) — usage dashboard design
- [Doueen/opencode-usage-extension](https://github.com/Doueen/opencode-usage-extension) — OpenCode quota parsing
- [BerriAI/litellm](https://github.com/BerriAI/litellm) — model pricing data source
