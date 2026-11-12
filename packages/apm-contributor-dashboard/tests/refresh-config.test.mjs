import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
    DEFAULT_REFRESH_INTERVAL_MINUTES,
    RATE_LIMIT_RETRY_MS,
    getNextRefreshDelayMs,
    resolveRefreshIntervalMs,
} from "../.apm/extensions/issue-monitor/refresh-config.mjs";

describe("refresh interval configuration", () => {
    it("defaults to 15 minutes", () => {
        assert.equal(DEFAULT_REFRESH_INTERVAL_MINUTES, 15);
        assert.equal(resolveRefreshIntervalMs(undefined), 900_000);
    });

    it("accepts a configured interval in minutes", () => {
        assert.equal(resolveRefreshIntervalMs("30"), 1_800_000);
    });

    it("rejects values below one minute", () => {
        assert.equal(resolveRefreshIntervalMs("0.5"), 900_000);
    });

    it("backs off for one hour after rate-limit errors", () => {
        assert.equal(
            getNextRefreshDelayMs(900_000, "GraphQL: API rate limit exceeded"),
            RATE_LIMIT_RETRY_MS,
        );
    });

    it("keeps the configured interval for other errors", () => {
        assert.equal(getNextRefreshDelayMs(1_800_000, "network error"), 1_800_000);
    });
});
