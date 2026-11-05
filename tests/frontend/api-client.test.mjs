import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createApiClient } from '../../static/api-client.js';

const turn = () => new Promise(resolve => setImmediate(resolve));
function fixture() {
  let clock = 1000;
  let sequence = 0;
  const timers = new Map(), calls = [];
  const client = createApiClient({ now: () => clock,
    fetchFn: (url, options) => new Promise(resolve => calls.push({ url, options, resolve })),
    setTimer: fn => { timers.set(++sequence, fn); return sequence; },
    clearTimer: id => timers.delete(id),
  });
  return { client, calls, timers, advance: ms => { clock += ms; } };
}

test('wake expires a request even when its browser timer and fetch never settle', async () => {
  const { client, calls, timers, advance } = fixture();
  const old = client.json('/api/quotas', { timeout: 60_000 });
  const rejected = assert.rejects(old, { name: 'TimeoutError' });
  advance(120_000); // Simulate system sleep; do not execute any timer callbacks.
  assert.equal(client.expire(), 1);
  await rejected;
  assert.equal(calls[0].options.signal.aborted, true);
  assert.equal(client.pending(), 0);
  assert.equal(timers.size, 0);
  const fresh = client.json('/api/quotas');
  calls[1].resolve({ ok: true, json: async () => ({ remaining: 20 }) });
  assert.deepEqual(await fresh, { remaining: 20 });
  // An uncooperative late response cannot change the rejected request.
  calls[0].resolve({ ok: true, json: async () => ({ remaining: 80 }) });
  await turn();
  assert.equal(client.pending(), 0);
});

test('timeout includes a stalled JSON body and releases all bookkeeping', async () => {
  const { client, calls, timers } = fixture();
  const job = client.json('/api/providers');
  const rejected = assert.rejects(job, { name: 'TimeoutError' });
  calls[0].resolve({ ok: true, json: () => new Promise(() => {}) });
  await turn();
  [...timers.values()][0]();
  await rejected;
  assert.equal(client.pending(), 0);
  assert.equal(timers.size, 0);
});

test('a late successful response is rejected before its result can be committed', async () => {
  const { client, calls, advance } = fixture();
  const job = client.json('/api/channels', { timeout: 1000 });
  const rejected = assert.rejects(job, { name: 'TimeoutError' });
  advance(2000);
  calls[0].resolve({ ok: true, json: async () => ['obsolete'] });
  await rejected;
});

test('superseded requests abort promptly even if fetch ignores AbortSignal', async () => {
  const { client, calls, timers } = fixture();
  const controller = new AbortController();
  const job = client.json('/api/usage/overview', { signal: controller.signal });
  const rejected = assert.rejects(job, { name: 'AbortError' });
  controller.abort();
  await rejected;
  assert.equal(calls[0].options.signal.aborted, true);
  assert.equal(client.pending(), 0);
  assert.equal(timers.size, 0);
});
