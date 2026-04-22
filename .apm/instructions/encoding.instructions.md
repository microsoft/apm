---
applyTo: "**"
description: "Cross-platform encoding rules — keep all source and CLI output within printable ASCII"
---

# Encoding Rules

## Constraint

All source code files and CLI output strings must stay within **printable ASCII** (U+0020–U+007E).

Do NOT use:
- Emojis (e.g. `🚀`, `✨`, `❌`)
- Unicode box-drawing characters (e.g. `─`, `│`, `┌`)
- Em dashes (`—`), en dashes (`–`), curly quotes (`"`, `"`, `'`, `'`)
- Any character outside the ASCII range (codepoint > U+007E)

**Why**: Windows `cp1252` terminals raise `UnicodeEncodeError: 'charmap' codec can't encode character` for any character outside cp1252. Keeping output within ASCII guarantees identical behaviour on every platform without dual-path fallback logic.

## Status symbol convention

Use ASCII bracket notation consistently across all CLI output, help text, and log messages:

| Symbol | Meaning              |
|--------|----------------------|
| `[+]`  | success / confirmed  |
| `[!]`  | warning              |
| `[x]`  | error                |
| `[i]`  | info                 |
| `[*]`  | action / processing  |
| `[>]`  | running / progress   |

These map directly to the `STATUS_SYMBOLS` dict in `src/apm_cli/utils/console.py`.

## Scope

This rule applies to:
- Python source files (`*.py`)
- CLI help strings and command output
- Markdown documentation and instruction files under `.github/`
- Shell scripts and CI workflow files

Exception: binary assets and third-party vendored files are excluded.
