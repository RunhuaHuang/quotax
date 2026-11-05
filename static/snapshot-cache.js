// Session snapshots are disposable; preferences and account data never enter here.
export const SNAPSHOT_KEY = "quotaboard_snapshot";
export const CACHE_RETENTION_MS = 30 * 60 * 1000;

export function isFresh(timestamp, now = Date.now()) {
  return Number.isFinite(timestamp) && timestamp <= now && now - timestamp < CACHE_RETENTION_MS;
}

function prune(snapshot, now) {
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) return {};
  const result = {};
  if (isFresh(snapshot.channelsSavedAt, now) && Array.isArray(snapshot.channels)) {
    const channels = snapshot.channels.filter(q => q && isFresh(q.updated_at, now));
    if (channels.length) Object.assign(result, { channels, channelsSavedAt: snapshot.channelsSavedAt,
      lastCheckedAt: snapshot.lastCheckedAt, lastUpdated: snapshot.lastUpdated });
  }
  if (isFresh(snapshot.localUsageSavedAt, now) && snapshot.localUsage) {
    Object.assign(result, { localUsage: snapshot.localUsage, localUsageSavedAt: snapshot.localUsageSavedAt });
  }
  return result;
}

function persist(storage, snapshot) {
  if (Object.keys(snapshot).length) {
    const serialized = JSON.stringify(snapshot);
    if (storage.getItem(SNAPSHOT_KEY) !== serialized) storage.setItem(SNAPSHOT_KEY, serialized);
  } else storage.removeItem(SNAPSHOT_KEY);
}

export function readSnapshot(storage, now = Date.now()) {
  try {
    const snapshot = prune(JSON.parse(storage.getItem(SNAPSHOT_KEY) || "null"), now);
    persist(storage, snapshot);
    return snapshot;
  } catch {
    try { storage.removeItem(SNAPSHOT_KEY); } catch { /* storage may be disabled */ }
    return {};
  }
}

export function writeSnapshot(storage, patch, now = Date.now()) {
  try {
    const snapshot = { ...readSnapshot(storage, now), ...patch };
    if (Object.hasOwn(patch, "channels")) snapshot.channelsSavedAt = now;
    if (Object.hasOwn(patch, "localUsage")) snapshot.localUsageSavedAt = now;
    persist(storage, prune(snapshot, now));
  } catch { /* storage may be disabled or full */ }
}
