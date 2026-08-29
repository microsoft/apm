---
title: Common errors
description: Symptoms, causes, and fixes for frequently reported APM errors.
sidebar:
  order: 11
---

This page lists error messages and silent failures that users report most
frequently, along with their cause and the shortest path to a fix.

---

### Cursor: "Config version must be a number"

**Symptom:** Cursor reports `Config version must be a number` or
`Failed to parse project hooks configuration`, or silently loads no project
hooks even though `apm install` succeeded and `.cursor/hooks.json` exists.

**Cause:** APM versions v0.14.1--v0.20.0 omitted the required top-level
`"version": 1` field from `.cursor/hooks.json`. Cursor rejects the entire
file when that field is absent.

**Fix:** Re-run hook integration to regenerate a valid config:

```bash
apm install --target cursor
```

Or, if you install all targets at once:

```bash
apm install
```

APM v0.21.0+ always writes `"version": 1` to `.cursor/hooks.json` on a
fresh install. Existing files that already contain a `"version"` key are
left untouched.

---

### Agent Plugin does not load in Copilot

**Symptom:** `apm install --target copilot` succeeds and writes the native
registration, but Copilot does not load the plugin.

**Cause:** APM does not discover, execute, or version-check the Copilot runtime
during install. Stable Copilot CLI `1.0.81` or newer is required to load APM's
live-directory projection. Older clients may instead copy plugins into private
state outside APM ownership.

**Fix:** Upgrade to stable GitHub Copilot CLI `1.0.81` or newer. If an older
client created a private copy, remove that copy with the client after upgrading;
APM uninstall and prune can retire only APM-owned registration state. See
[Install Agent Plugins for Copilot](../../consumer/copilot-agent-plugins/#requirements).
