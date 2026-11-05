import { test } from 'node:test';
import assert from 'node:assert/strict';
import { CACHE_RETENTION_MS as TTL, SNAPSHOT_KEY, readSnapshot, writeSnapshot } from '../../static/snapshot-cache.js';

function storage() {
  const data = new Map([['quotaboard_prefs', '{"theme":"dark"}']]);
  return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value),
    removeItem: key => data.delete(key) };
}

test('snapshot expires exactly at 30 minutes and leaves preferences intact', () => {
  const s = storage();
  writeSnapshot(s, { channels: [{ id: 'a', updated_at: 100 }], localUsage: { total: 10 } }, 100);
  assert.equal(readSnapshot(s, 100 + TTL - 1).channels.length, 1);
  assert.deepEqual(readSnapshot(s, 100 + TTL), {});
  assert.equal(s.getItem(SNAPSHOT_KEY), null);
  assert.equal(s.getItem('quotaboard_prefs'), '{"theme":"dark"}');
});

test('refreshing local usage or another channel cannot renew old quota values', () => {
  const s = storage();
  writeSnapshot(s, { channels: [{ id: 'a', updated_at: 100 }], localUsage: { total: 10 } }, 100);
  writeSnapshot(s, { localUsage: { total: 20 } }, 100 + TTL - 1);
  assert.equal(readSnapshot(s, 100 + TTL).channels, undefined);
  assert.equal(readSnapshot(s, 100 + TTL).localUsage.total, 20);
  writeSnapshot(s, { channels: [{ id: 'a', updated_at: 100 }, { id: 'b', updated_at: 100 + TTL }] }, 100 + TTL);
  assert.deepEqual(readSnapshot(s, 100 + TTL).channels.map(q => q.id), ['b']);
});

test('refreshing quotas cannot renew a local usage snapshot', () => {
  const s = storage();
  writeSnapshot(s, { localUsage: { total: 10 } }, 100);
  writeSnapshot(s, { channels: [{ id: 'a', updated_at: 100 + TTL }] }, 100 + TTL);
  assert.equal(readSnapshot(s, 100 + TTL).localUsage, undefined);
});

test('legacy, malformed and future-dated snapshots are discarded', () => {
  for (const raw of ['broken', 'null', '[]', '{"channels":[{"id":"a"}]}',
    JSON.stringify({ channels: [{ id: 'a', updated_at: 200 }], channelsSavedAt: 200 })]) {
    const s = storage();
    s.setItem(SNAPSHOT_KEY, raw);
    assert.deepEqual(readSnapshot(s, 100), {});
    assert.equal(s.getItem(SNAPSHOT_KEY), null);
  }
});
