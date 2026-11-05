// Bound the entire request, including body decoding. A wall-clock deadline is
// checked again on wake: browser timers may have been suspended during sleep.
export function createApiClient({ fetchFn = (...args) => fetch(...args), now = () => Date.now(),
  setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
  const active = new Set();

  function expire() {
    let count = 0;
    for (const entry of active) {
      if (now() >= entry.deadline) {
        count += 1;
        entry.cancel(new DOMException("Request timed out", "TimeoutError"));
      }
    }
    return count;
  }

  function json(url, { timeout = 15_000, signal, ...options } = {}) {
    expire();
    if (signal?.aborted) return Promise.reject(signal.reason);
    const controller = new AbortController();
    let timer;
    let rejectRequest;
    const cancelled = new Promise((_, reject) => { rejectRequest = reject; });
    const entry = {
      deadline: now() + timeout,
      cancel(error) {
        active.delete(entry);
        rejectRequest(error);
        controller.abort(error);
      },
    };
    const onAbort = () => entry.cancel(signal.reason);
    signal?.addEventListener("abort", onAbort, { once: true });
    active.add(entry);
    timer = setTimer(() => entry.cancel(new DOMException("Request timed out", "TimeoutError")), timeout);
    const operation = (async () => {
      controller.signal.throwIfAborted();
      const response = await fetchFn(url, { ...options, cache: "no-store", signal: controller.signal });
      if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try { detail = (await response.json()).detail || detail; } catch { /* use HTTP status */ }
        throw new Error(String(detail));
      }
      const data = await response.json();
      // A response delivered just after unfreezing must not revive expired data.
      expire();
      controller.signal.throwIfAborted();
      return data;
    })();
    return Promise.race([operation, cancelled]).finally(() => {
      active.delete(entry);
      clearTimer(timer);
      signal?.removeEventListener("abort", onAbort);
    });
  }
  return { json, expire, pending: () => active.size };
}

const client = createApiClient();
export const apiJSON = client.json;
export const expireRequests = client.expire;
