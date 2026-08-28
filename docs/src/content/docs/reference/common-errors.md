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

### Agent Plugin: "needs GitHub Copilot CLI >=1.0.81-8"

**Symptom:** `apm install --target copilot` refuses an Agent Plugins 1.0
package with `Agent Plugins v1.0.0 packages need GitHub Copilot CLI
>=1.0.81-8 ... detected <version>`, and nothing is written. With no `copilot`
binary on `PATH`, the message instead reads `GitHub Copilot CLI was not found
on PATH`.

**Cause:** Native registration loads the plugin live from `apm_modules`, which
only the qualified Copilot CLI build supports. Older clients copy the plugin
into private Copilot state, so APM fails closed instead of degrading silently.
The same rule applies to non-Copilot targets.

For the package being actively installed this is fatal and the project tree is
left byte-identical. Two neighbours are treated as recoverable instead: an
*already-installed* Agent Plugin whose registration merely cannot be refreshed
(Copilot CLI absent or below the floor) keeps its existing registration
untouched and the command continues with a warning; and a project whose targets
exclude `copilot` skips each Agent Plugin dependency with a warning and installs
the rest of the batch.

**Fix:** Install GitHub Copilot CLI `1.0.81-8` or newer (stable `1.0.81`
qualifies), then re-run. See
[Install Agent Plugins for Copilot](../../consumer/copilot-agent-plugins/#requirements).
