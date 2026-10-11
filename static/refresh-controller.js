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
export function createPoller({ run, delay, enabled, setTimer = setTimeout, clearTimer = clearTimeout }) {
  let timer = null;
  let pending = null;
  function stop() { clearTimer(timer); timer = null; }
  function schedule() {
    stop();
    if (!pending && enabled()) timer = setTimer(tick, delay());
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
  return { schedule, stop, tick };
}
