export const DEFAULT_REFRESH_INTERVAL_MINUTES = 15;
export const MIN_REFRESH_INTERVAL_MINUTES = 1;
export const RATE_LIMIT_RETRY_MS = 60 * 60 * 1000;

export function resolveRefreshIntervalMs(value) {
    const minutes = Number(value);
    if (!Number.isFinite(minutes) || minutes < MIN_REFRESH_INTERVAL_MINUTES) {
        return DEFAULT_REFRESH_INTERVAL_MINUTES * 60 * 1000;
    }
    return Math.round(minutes * 60 * 1000);
}

export function getNextRefreshDelayMs(refreshIntervalMs, error) {
    if (/rate limit/i.test(String(error || ""))) {
        return Math.max(refreshIntervalMs, RATE_LIMIT_RETRY_MS);
    }
    return refreshIntervalMs;
}
