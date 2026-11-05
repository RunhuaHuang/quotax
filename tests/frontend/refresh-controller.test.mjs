import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createQuotaRefresher, createPoller, bindRefreshLifecycle } from '../../static/refresh-controller.js';

function deferred() {
  let resolve, reject;
  const promise = new Promise((a,b) => { resolve=a; reject=b; });
  return {promise, resolve, reject};
}
const turn = () => new Promise((r) => setImmediate(r));
function fixture(concurrency = 4) {
  const calls = [], displayed = new Map();
  const client = createQuotaRefresher({ concurrency,
    fetchChannel(id, options) { const d = deferred(); calls.push({id, options, ...d}); return d.promise; },
    onResult(id, data) { displayed.set(id, data); },
  });
  return {client, calls, displayed};
}

test('slow channel does not hold back fast card; global and single requests share one result', async () => {
  const {client,calls,displayed}=fixture();
  const a=client.refresh('a',{force:true}), b=client.refresh('b');
  const single=client.refresh('a',{force:true});
  assert.strictEqual(single,a);
  calls[0].resolve(20); await single;
  assert.equal(displayed.get('a'),20);
  assert.equal(displayed.has('b'),false);
  calls[1].resolve(30); await b;
  assert.equal(displayed.get('a'),20);
});

test('automatic refresh cannot abort or downgrade an active manual refresh', async () => {
  const {client,calls,displayed}=fixture();
  const manual=client.refresh('a',{force:true});
  const automatic=client.refresh('a');
  assert.strictEqual(manual,automatic);
  assert.equal(calls.length,1);
  assert.equal(calls[0].options.signal.aborted,false);
  calls[0].resolve(20); await automatic;
  assert.equal(displayed.get('a'),20);
});

test('multiple manual clicks upgrade an existing cache read exactly once', async () => {
  const {client,calls,displayed}=fixture();
  const automatic=client.refresh('a');
  const manual=client.refresh('a',{force:true});
  assert.strictEqual(client.refresh('a',{force:true}),manual);
  calls[0].resolve(10); await automatic; await turn();
  assert.equal(calls.length,2);
  assert.equal(calls[1].options.force,true);
  calls[1].resolve(20); await manual;
  assert.equal(displayed.get('a'),20);
});

test('late results after edit/delete never restore old values, including queued upgrades', async () => {
  const {client,calls,displayed}=fixture();
  const old=client.refresh('a');
  const upgrade=client.refresh('a',{force:true});
  client.invalidate('a');
  const fresh=client.refresh('a');
  calls[1].resolve(20); await fresh;
  calls[0].resolve(10); await old; await upgrade;
  assert.equal(displayed.get('a'),20);
  assert.equal(calls.length,2);
});

test('concurrency is bounded and a failed channel releases its slot', async () => {
  const {client,calls}=fixture(2);
  const jobs=['a','b','c'].map(id=>client.refresh(id).catch(()=>{}));
  assert.equal(calls.length,2);
  calls[0].reject(new Error('offline')); await turn();
  assert.equal(calls.length,3);
  calls[1].resolve(1); calls[2].resolve(1); await Promise.all(jobs);
});

test('polling waits for completion; hidden/offline stops scheduling and returning resumes once', async () => {
  let enabled=true, count=0;
  const timers=new Map(); let seq=0;
  let pending=deferred();
  const poller=createPoller({run:()=>{count++;return pending.promise;}, delay:()=>30,
    enabled:()=>enabled,setTimer:fn=>{timers.set(++seq,fn);return seq;},clearTimer:id=>timers.delete(id)});
  poller.schedule(); assert.equal(timers.size,1);
  const round=poller.tick(); await turn();
  poller.schedule(); poller.tick(); assert.equal(count,1); assert.equal(timers.size,0);
  enabled=false; pending.resolve(); await round; assert.equal(timers.size,0);
  enabled=true; pending=deferred(); const resumed=poller.tick(); await turn(); assert.equal(count,2);
  pending.resolve(); await resumed; assert.equal(timers.size,1);
  poller.stop(); assert.equal(timers.size,0);
});

test('obsolete network failures cannot overwrite a newer successful card', async () => {
  const {client,calls,displayed}=fixture();
  let errors=0;
  const old=client.refresh('a').catch(()=>{errors++; displayed.set('a','error');});
  client.invalidate('a');
  const fresh=client.refresh('a');
  calls[1].resolve(20); await fresh;
  calls[0].reject(new TypeError('late body decoding failure')); await old;
  assert.equal(errors,0);
  assert.equal(displayed.get('a'),20);
});

test('wake recovery waits for the expired round then starts exactly one new round', async () => {
  let first = deferred(), second = deferred(), count = 0;
  const poller = createPoller({ run: () => ++count === 1 ? first.promise : second.promise,
    delay: () => 30_000, enabled: () => true, setTimer: () => 1, clearTimer() {} });
  poller.tick(); await turn();
  const recovery = poller.resume({ restart: true });
  assert.strictEqual(poller.resume({ restart: true }), recovery);
  first.reject(new Error('expired after sleep')); await turn();
  assert.equal(count, 2);
  second.resolve(); await recovery;
  poller.stop();
});

test('lifecycle covers frozen pages, bfcache, focus, quick reconnect and missed heartbeat', () => {
  const page = new EventTarget(), host = new EventTarget();
  page.hidden = false;
  let clock = 1000, connected = true, expires = 0, heartbeat, stopped = 0;
  const rounds = [];
  const cleanup = bindRefreshLifecycle({ page, host, now: () => clock, online: () => connected,
    recover: () => { const value = expires; expires = 0; return value; },
    poller: { stop: () => stopped++, resume: (options = {}) => rounds.push(options) },
    afterTurn: fn => fn(),
    setHeartbeat: fn => { heartbeat = fn; return 1; }, clearHeartbeat() {},
  });
  page.dispatchEvent(new Event('resume'));
  host.dispatchEvent(new Event('focus'));
  host.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: true }));
  assert.equal(rounds.length, 1); // A single thaw can emit all three events.
  connected = false; host.dispatchEvent(new Event('offline'));
  connected = true; host.dispatchEvent(new Event('online'));
  assert.equal(rounds.length, 2); // Even if offline lasted less than one second.
  page.hidden = true; page.dispatchEvent(new Event('visibilitychange'));
  page.dispatchEvent(new Event('freeze'));
  clock += 120_000; expires = 1; page.hidden = false;
  page.dispatchEvent(new Event('resume'));
  assert.equal(rounds.at(-1).restart, true);
  clock += 120_000; expires = 1; heartbeat(); // Sleep without a visibility event.
  assert.equal(rounds.at(-1).restart, true);
  clock += 15_000; expires = 1; heartbeat(); // A timed-out request without sleep.
  assert.equal(rounds.at(-1).restart, true);
  assert.equal(stopped, 3);
  cleanup();
  const before = rounds.length;
  host.dispatchEvent(new Event('online'));
  assert.equal(rounds.length, before);
});
