// One request per channel. Manual refresh upgrades a cached request without
// cancelling it; generations prevent deleted/reconfigured cards coming back.
export function createQuotaRefresher({ fetchChannel, onResult, concurrency = 4 }) {
  const active = new Map();
  const generations = new Map();
  const waiting = [];
  let running = 0;
  async function limited(work) {
    if (running >= concurrency) await new Promise((resolve) => waiting.push(resolve));
    else running += 1;
    try { return await work(); }
    finally {
      const next = waiting.shift();
      if (next) next();
      else running -= 1;
    }
  }
  function invalidate(id) {
    generations.set(id, (generations.get(id) || 0) + 1);
    active.get(id)?.controller.abort();
    active.delete(id);
  }
  function refresh(id, options = {}) {
    const force = Boolean(options.force);
    const current = active.get(id);
    if (current) {
      if (current.upgrade) return current.upgrade;
      if (!force || current.force) return current.promise;
      const generation = generations.get(id) || 0;
      current.upgrade = current.promise.catch(() => {}).then(() => {
        if ((generations.get(id) || 0) === generation) return refresh(id, options);
      });
      return current.upgrade;
    }
    const generation = generations.get(id) || 0;
    const entry = { force, controller: new AbortController(), promise: null };
    active.set(id, entry);
    entry.promise = limited(async () => {
      if (entry.controller.signal.aborted) return;
      const data = await fetchChannel(id, { ...options, signal: entry.controller.signal });
      if ((generations.get(id) || 0) === generation && !entry.controller.signal.aborted) {
        onResult(id, data);
      }
      return data;
    }).catch((error) => {
      // Fetch/body decoding may fail with TypeError or SyntaxError after abort.
      // Obsolete failures must be ignored just like obsolete successful results.
      if ((generations.get(id) || 0) !== generation || entry.controller.signal.aborted) return;
      throw error;
    }).finally(() => {
      if (active.get(id) === entry) active.delete(id);
    });
    return entry.promise;
  }
  return { refresh, invalidate, busy: (id) => active.has(id) };
}

// Completion-based polling: changing tabs/options cannot overlap a slow round.
export function createPoller({ run, delay, enabled, setTimer = setTimeout, clearTimer = clearTimeout,
  now = () => Date.now() }) {
  let timer = null;
  let pending = null;
  let recovery = null;
  let nextAt = null;
  function stop() { clearTimer(timer); timer = null; nextAt = null; }
  function schedule() {
    stop();
    if (!pending && enabled()) {
      const wait = Math.max(1000, delay());
      nextAt = now() + wait;
      timer = setTimer(tick, wait);
    }
  }
  function tick() {
    stop();
    if (pending) return pending;
    if (!enabled()) return Promise.resolve();
    pending = Promise.resolve().then(run).catch(() => {}).finally(() => {
      pending = null;
      schedule();
    });
    return pending;
  }
  function resume({ restart = false } = {}) {
    if (recovery) return recovery;
    if (!restart || !pending) return tick();
    // Expired requests settle first, releasing per-channel and view locks.
    recovery = pending.then(tick).finally(() => { recovery = null; });
    return recovery;
  }
  return { schedule, stop, tick, resume, nextAt: () => nextAt };
}

// Attach before starting any network work. Repeated wake events share one round;
// a suspended timer cannot make an old in-flight request block the page forever.
export function bindRefreshLifecycle({ poller, recover, update = () => {},
  page = document, host = window, online = () => navigator.onLine !== false,
  now = () => Date.now(), setHeartbeat = setInterval, clearHeartbeat = clearInterval,
  afterTurn = (fn) => setTimeout(fn, 0) }) {
  let lastWake = -Infinity;
  let lastBeat = now();
  let disposed = false;
  const listeners = [];
  function listen(target, event, fn) {
    target.addEventListener(event, fn);
    listeners.push(() => target.removeEventListener(event, fn));
  }
  function wake() {
    update();
    const expired = recover();
    if (page.hidden || !online()) { poller.stop(); return; }
    const time = now();
    if (!expired && time - lastWake < 1000) return;
    lastWake = time;
    resume(expired);
  }
  function resume(expired) {
    // Manual refreshes and startup can also own shared promises outside poller.
    // Let their rejection/finally chains release locks before starting again.
    if (expired) afterTurn(() => { if (!disposed) poller.resume({ restart: true }); });
    else poller.resume();
  }
  listen(page, "visibilitychange", () => page.hidden ? poller.stop() : wake());
  listen(page, "freeze", () => poller.stop());
  listen(page, "resume", wake);
  listen(host, "pagehide", () => poller.stop());
  listen(host, "pageshow", (event) => { if (event.persisted) wake(); });
  listen(host, "focus", wake);
  listen(host, "online", wake);
  listen(host, "offline", () => { lastWake = -Infinity; poller.stop(); update(); });
  const heartbeat = setHeartbeat(() => {
    const time = now();
    const slept = time - lastBeat > 30_000;
    lastBeat = time;
    if (!page.hidden) {
      update();
      if (slept) wake();
      else {
        const expired = recover();
        if (expired) resume(expired);
      }
    }
  }, 15_000);
  return () => { disposed = true; clearHeartbeat(heartbeat); listeners.forEach((remove) => remove()); };
}
