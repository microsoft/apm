---
description: Report this plugin's bundled resource
argument-hint: "[qualification-token]"
disable-model-invocation: true
allowed-tools: Bash(node:*)
---

!`node "${CLAUDE_PLUGIN_ROOT}/scripts/context.mjs" "$ARGUMENTS"`

Return the command's JSON exactly without invoking additional tools.
