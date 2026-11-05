import { test, expect, chromium } from '@playwright/test';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
let server, port, data, baseURL;

async function startServer() {
  const python = process.env.QUOTAX_TEST_PYTHON || join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  server = spawn(python, ['tests/browser/server.py', String(port), data], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
  let output = '';
  server.stdout.on('data', chunk => { output += chunk; });
  server.stderr.on('data', chunk => { output += chunk; });
  for (let attempt = 0; attempt < 200; attempt++) {
    if (server.exitCode !== null) throw new Error(`Test server exited: ${output}`);
    try { if ((await fetch(`${baseURL}/healthz`)).ok) return; } catch { /* starting */ }
    await sleep(50);
  }
  throw new Error(`Test server did not start: ${output}`);
}
async function stopServer() {
  if (!server || server.exitCode !== null) return;
  const exited = once(server, 'exit');
  server.kill('SIGTERM');
  await exited;
}

async function lifecycleBrowser() {
  // Playwright's default focus emulation keeps pages visible and prevents real
  // freezing. Attach without those overrides to an isolated disposable profile.
  const profile = await mkdtemp(join(tmpdir(), 'quotax-lifecycle-'));
  const chrome = process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
    : process.platform === 'win32' ? join(process.env.PROGRAMFILES, 'Google/Chrome/Application/chrome.exe')
      : '/opt/google/chrome/chrome';
  const executable = process.env.QUOTAX_TEST_CHROME_EXECUTABLE
    || (process.env.QUOTAX_TEST_BROWSER_CHANNEL === 'chrome' ? chrome : chromium.executablePath());
  const processHandle = spawn(executable, ['--headless=new', '--no-sandbox', '--remote-debugging-port=0',
    `--user-data-dir=${profile}`, '--no-first-run', '--no-default-browser-check', '--disable-background-networking'],
    { stdio: 'ignore' });
  let browser;
  async function close() {
    if (processHandle.exitCode === null) {
      const exited = once(processHandle, 'exit');
      processHandle.kill('SIGTERM');
      await exited;
    }
    await browser?.close();
    await rm(profile, { recursive: true, force: true });
  }
  try {
    for (let attempt = 0; attempt < 200; attempt++) {
      let address;
      try { address = await readFile(join(profile, 'DevToolsActivePort'), 'utf8'); } catch { /* starting */ }
      if (address) {
        browser = await chromium.connectOverCDP(`http://127.0.0.1:${address.split('\n')[0]}`, { noDefaults: true });
        const context = browser.contexts()[0];
        return { context, page: context.pages()[0], close };
      }
      if (processHandle.exitCode !== null) throw new Error('Lifecycle browser exited before startup');
      await sleep(50);
    }
    throw new Error('Lifecycle browser did not start');
  } catch (error) { await close(); throw error; }
}

test.beforeAll(async () => {
  const socket = createServer();
  socket.listen(0, '127.0.0.1'); await once(socket, 'listening');
  port = socket.address().port; await new Promise(resolve => socket.close(resolve));
  baseURL = `http://127.0.0.1:${port}`;
  data = await mkdtemp(join(tmpdir(), 'quotax-browser-'));
  await startServer();
});
test.afterAll(async () => { await stopServer(); if (data) await rm(data, { recursive: true, force: true }); });

async function mockAccounts(page, options = {}) {
  const state = { quotaCalls: [], providerCalls: 0, held: [], holdNext: false, balance: 80, collects: 0 };
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  state.errors = errors;
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const send = json => route.fulfill({ json });
    if (url.pathname === '/api/providers') {
      state.providerCalls++;
      if (options.failProvidersFirst && state.providerCalls === 1) {
        return route.fulfill({ status: 503, json: { detail: 'Temporarily unavailable' } });
      }
      return route.continue();
    }
    if (url.pathname === '/api/channels') return send([{ id: 'demo', type: 'deepseek', name: 'Test account', enabled: true }]);
    if (url.pathname === '/api/quotas') {
      state.quotaCalls.push(url.searchParams.get('force') === '1');
      if (state.holdNext) { state.holdNext = false; state.held.push(route); return; }
      const now = Date.now();
      return send({ channels: [{ id: 'demo', type: 'deepseek', name: 'Test account', category: 'balance',
        status: 'ok', updated_at: now, windows: [], amount: { value: state.balance, currency: 'USD', label: `$${state.balance}.00` },
        cache: { hit: false, source: 'live', next_query_at: now + 300_000 },
      }] });
    }
    if (url.pathname === '/api/local-usage') return send({ days: 14, sources: [], source: 'database' });
    if (url.pathname === '/api/usage/collect') { state.collects++; return send({ total_new: 1 }); }
    if (url.pathname === '/api/usage/overview') return send({ total_tokens: 123, requests: 1, sessions: 1 });
    if (url.pathname === '/api/usage/trend' || url.pathname === '/api/usage/models') return send([]);
    if (url.pathname === '/api/usage/log') return send({ rows: [], limit: 200, offset: 0, has_more: false });
    if (url.pathname === '/api/usage/filters') return send({ sources: [], models: [] });
    return route.continue();
  });
  return state;
}

async function ready(page) {
  await page.goto(baseURL);
  await expect(page.locator('.balance-number')).toHaveText('80.00');
  await page.evaluate(() => { window.sameTabMarker = 'keep-me'; });
}

test('30-minute cache cleanup works with automatic refresh disabled', async ({ page }) => {
  const state = await mockAccounts(page);
  await page.clock.install();
  await ready(page);
  await page.locator('label').filter({ has: page.locator('#autoRefresh') }).click();
  const calls = state.quotaCalls.length;
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('quotaboard_snapshot'))).not.toBeNull();
  await page.clock.fastForward(30 * 60 * 1000 + 15_000);
  await expect(page.locator('.balance-number')).toHaveCount(0);
  await expect(page.locator('.card')).toContainText('30');
  expect(await page.evaluate(() => sessionStorage.getItem('quotaboard_snapshot'))).toBeNull();
  await expect(page.locator('#autoRefresh')).not.toBeChecked();
  expect(state.quotaCalls.length).toBe(calls);
  expect(state.errors).toEqual([]);
});

test('initial service failure can be retried in the same tab', async ({ page }) => {
  const state = await mockAccounts(page, { failProvidersFirst: true });
  await page.goto(baseURL);
  await expect(page.locator('[data-action="retry-init"]')).toBeVisible();
  await page.evaluate(() => { window.sameTabMarker = 'keep-me'; });
  await page.locator('[data-action="retry-init"]').click();
  await expect(page.locator('.balance-number')).toHaveText('80.00');
  expect(state.providerCalls).toBe(2);
  expect(await page.evaluate(() => window.sameTabMarker)).toBe('keep-me');
  expect(state.errors).toEqual([]);
});

test('frozen tab recovers an expired request and accepts another manual refresh', async () => {
  const native = await lifecycleBrowser();
  const { page, context } = native;
  try {
    const state = await mockAccounts(page);
    await ready(page);
    let reportFrozen;
    const frozen = new Promise(resolve => { reportFrozen = resolve; });
    await page.exposeFunction('reportFrozen', () => reportFrozen());
    await page.evaluate(() => document.addEventListener('freeze', () => window.reportFrozen(), { once: true }));
    state.holdNext = true;
    await page.locator('#btnRefresh').click();
    await expect.poll(() => state.held.length).toBe(1);
    const cdp = await context.newCDPSession(page);
    // Move wall time without firing timers, then exercise real Chromium freeze/resume.
    await page.evaluate(() => { const realNow = Date.now; Date.now = () => realNow() + 120_000; });
    await cdp.send('Page.setWebLifecycleState', { state: 'frozen' });
    await Promise.race([frozen, sleep(3000).then(() => { throw new Error('Chromium did not freeze the page'); })]);
    state.balance = 37;
    await cdp.send('Page.setWebLifecycleState', { state: 'active' });
    await page.bringToFront();
    await expect(page.locator('.balance-number')).toHaveText('37.00');
    await expect(page.locator('#btnRefresh')).not.toHaveAttribute('aria-busy', 'true');
    // Restore normal automation focus after verifying the real freeze/resume.
    await cdp.send('Emulation.setFocusEmulationEnabled', { enabled: true });
    state.balance = 24;
    await page.locator('#btnRefresh').click();
    await expect(page.locator('.balance-number')).toHaveText('24.00');
    await expect(page.locator('#btnRefresh')).not.toHaveAttribute('aria-busy', 'true');
    expect(await page.evaluate(() => window.sameTabMarker)).toBe('keep-me');
    expect(state.errors).toEqual([]);
  } finally { await native.close(); }
});

test('offline/online recovery updates the existing tab', async ({ page, context }) => {
  const state = await mockAccounts(page);
  await ready(page);
  await context.setOffline(true);
  await expect(page.locator('#runtimeStatus')).toHaveAttribute('data-state', 'offline');
  state.balance = 42;
  await context.setOffline(false);
  await expect(page.locator('.balance-number')).toHaveText('42.00');
  expect(await page.evaluate(() => window.sameTabMarker)).toBe('keep-me');
  expect(state.errors).toEqual([]);
});

test('usage view refresh collects new data and returning to quota refreshes that view', async ({ page }) => {
  const state = await mockAccounts(page);
  await ready(page);
  await page.locator('[data-view="usage"]').click();
  await expect(page.locator('#usageCollectBtn')).toBeVisible();
  await page.locator('#btnRefresh').click();
  await expect.poll(() => state.collects).toBe(1);
  await expect(page.locator('#btnRefresh')).not.toHaveAttribute('aria-busy', 'true');
  state.balance = 19;
  await page.locator('[data-view="quota"]').click();
  await expect(page.locator('.balance-number')).toHaveText('19.00');
  expect(state.errors).toEqual([]);
});

test('same tab reconnects after an actual server restart and shows runtime paths', async ({ page }) => {
  const state = await mockAccounts(page);
  await ready(page);
  const before = await page.evaluate(async () => (await (await fetch('/api/runtime')).json()).instance_id);
  await stopServer();
  await page.locator('#runtimeDiagnostics summary').click();
  await expect(page.locator('#runtimeStatus')).toHaveAttribute('data-state', 'unreachable');
  await startServer();
  state.balance = 12;
  await page.locator('#btnRefresh').click();
  await expect(page.locator('.balance-number')).toHaveText('12.00');
  await expect(page.locator('#runtimeStatus')).toHaveAttribute('data-state', 'connected');
  const after = await page.evaluate(async () => (await (await fetch('/api/runtime')).json()).instance_id);
  expect(after).not.toBe(before);
  await expect(page.locator('#runtimeDetails')).toContainText(data);
  expect(await page.evaluate(() => window.sameTabMarker)).toBe('keep-me');
  expect(state.errors).toEqual([]);
});

test('new server version is visible without discarding unsaved configuration', async ({ page }) => {
  const state = await mockAccounts(page);
  await ready(page);
  await page.locator('#btnConfig').click();
  await page.locator('#fName').fill('Unsaved account name');
  await page.route('**/api/runtime', route => route.fulfill({ json: {
    version: '999.0.0', instance_id: 'upgraded', started_at: Date.now(), collection: {},
  } }));
  await page.evaluate(() => document.querySelector('#btnRuntimeRefresh').click());
  await expect(page.locator('#versionNotice')).toBeVisible();
  await expect(page.locator('#fName')).toHaveValue('Unsaved account name');
  expect(state.errors).toEqual([]);
});
