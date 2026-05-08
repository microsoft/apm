---
name: cli-logging-ux
description: >
  Use this skill when editing or creating CLI output, logging, warnings,
  error messages, progress indicators, or diagnostic summaries in the APM
  codebase. Activate whenever code touches console helpers (_rich_success,
  _rich_warning, _rich_error, _rich_info, _rich_echo), DiagnosticCollector,
  STATUS_SYMBOLS, CommandLogger, or any user-facing terminal output — even
  if the user doesn't mention "logging" or "UX" explicitly.
---

[CLI Logging UX expert persona](../../agents/cli-logging-expert.agent.md)

# CLI Logging & Developer Experience

## Decision framework

Apply these three tests to every piece of user-facing output. If a message fails any test, redesign it.

### 1. The "So What?" Test

Every warning must answer: *what should the user do about this?*

```
# Fails — not actionable, user can't do anything
Sub-skill 'my-skill' from 'my-package' overwrites existing skill

# Passes — tells the user exactly what to do
Skipping my-skill — local file exists (not managed by APM). Use 'apm install --force' to overwrite.
```

If the user can't act on it, it's not a warning — it's noise. Demote to `--verbose` or remove.

### 2. The Traffic Light Rule

Use color semantics consistently. Never use a warning color for an informational state.

| Color | Helper | Meaning | When to use |
|-------|--------|---------|-------------|
| Green | `_rich_success()` | Success / completed | Operation finished as expected |
| Yellow | `_rich_warning()` | User action needed | Something requires user decision |
| Red | `_rich_error()` | Error / failure | Operation failed, cannot continue |
| Blue | `_rich_info()` | Informational | Status updates, progress, summaries |
| Dim | `_rich_echo(color="dim")` | Secondary detail | Verbose-mode details, grouping headers |

### 3. The Newspaper Test

Can the user scan output like headlines? Top-level = what happened. Details = drill down.

```
# Bad — warnings break the visual flow between status and summary
[checkmark] package-name
[warning] something happened
[warning] something else happened
  [tree] 3 skill(s) integrated

# Good — clean tree, diagnostics at the end
[checkmark] package-name
  [tree] 3 skill(s) integrated

── Diagnostics ──
  [warning] 2 skills replaced by a different package (last installed wins)
    Run with --verbose to see details
```

## Inline output vs deferred diagnostics

### Use inline output for:
- Success confirmations (`_rich_success`)
- Progress updates (`_rich_info` with indented `└─` prefix)
- Errors that halt the current operation (`_rich_error`)

### Use DiagnosticCollector for:
- Warnings that apply across multiple packages (collisions, overwrites)
- Issues the user should know about but that don't stop the operation
- Anything that would repeat N times in a loop

```python
# Bad — inline warning repeated per file, clutters output
for file in files:
    if collision:
        _rich_warning(f"Skipping {file}...")

# Good — collect during loop, render grouped summary at the end
for file in files:
    if collision:
        diagnostics.skip(file, package=pkg_name)

# Later, after the loop:
if diagnostics.has_diagnostics:
    diagnostics.render_summary()
```

DiagnosticCollector categories: `skip()` for collisions, `overwrite()` for cross-package replacements, `warn()` for general warnings, `error()` for failures.

## Console helper conventions

Always use the helpers from `apm_cli.utils.console` — never raw `print()` or bare `click.echo()`.

**Emojis are banned.** Never use emoji characters anywhere in CLI output — not in messages, symbols, help text, or status indicators. Use ASCII text symbols exclusively via `STATUS_SYMBOLS`.

```python
from apm_cli.utils.console import (
    _rich_success, _rich_error, _rich_warning, _rich_info, _rich_echo
)

_rich_success("Installed 3 APM dependencies")        # green, bold
_rich_info("  └─ 2 prompts integrated → .github/prompts/")  # blue
_rich_warning("Config drift detected — re-run apm install")  # yellow
_rich_error("Failed to download package")              # red
_rich_echo("    [pkg-name]", color="dim")              # dim, for verbose details
```

Use `STATUS_SYMBOLS` dict with `symbol=` parameter for consistent ASCII prefixes:
```python
_rich_info("Starting operation...", symbol="gear")     # renders as "[*] Starting operation..."
```

## Output structure pattern

Follow this visual hierarchy for multi-package operations:

```
[checkmark] package-name-1                      # _rich_success — download/copy ok
  [tree] 2 prompts integrated → .github/prompts/     # _rich_info — indented summary
  [tree] 1 skill(s) integrated → .github/skills/
[checkmark] package-name-2
  [tree] 1 instruction(s) integrated → .github/instructions/

── Diagnostics ──                         # Only if diagnostics.has_diagnostics
  [warning] N files skipped — ...                   # Grouped by category
    Run with --verbose to see details

Installed 2 APM dependencies              # _rich_success — final summary
```

## Content-awareness principle

Before reporting changes, check if anything actually changed. Don't report no-ops.

```python
# Bad — always copies and reports, even when content is identical
shutil.rmtree(target)
shutil.copytree(source, target)
_rich_info(f"  └─ Skill updated")

# Good — skip when content matches
if SkillIntegrator._dirs_equal(source, target):
    continue  # Nothing changed, nothing to report
```

## CommandLogger Architecture

APM is a large and growing CLI with 10+ commands, 8+ integrators, and dozens of output sites. The logging architecture enforces **Separation of Concerns**: commands declare *what* happened; the logger decides *how* to render it. This keeps output consistent, testable, and evolvable without shotgun surgery across command files.

### The three layers

```
┌─────────────────────────────────────────────────────┐
│  Command layer  (install.py, pack.py, audit.py …)   │
│  Calls: logger.success(), logger.tree_item(), …      │
│  NEVER calls: _rich_*, click.echo(), print()         │
├─────────────────────────────────────────────────────┤
│  Logger layer   (command_logger.py)                  │
│  CommandLogger ← InstallLogger, future subclasses    │
│  Owns: verbose gating, symbol choice, indentation    │
│  Delegates to: _rich_* helpers                       │
├─────────────────────────────────────────────────────┤
│  Rendering layer (console.py)                        │
│  _rich_echo, _rich_success, _rich_error, …           │
│  Owns: Rich/colorama fallback, color, STATUS_SYMBOLS │
└─────────────────────────────────────────────────────┘
```

Changes to output style (colors, symbols, indentation) happen in the **logger or rendering layer only** — command code is untouched. New output patterns (e.g. a tree sub-item, a package metadata line) become new logger methods, not ad-hoc format strings in commands.

### Base class: `CommandLogger`

`src/apm_cli/core/command_logger.py` — base for all commands.

| Method | Purpose | When to use |
|--------|---------|-------------|
| `start(msg, symbol=)` | Operation start | Beginning of a command |
| `progress(msg, symbol=)` | Status update with `[i]` prefix | Mid-operation phase changes |
| `success(msg, symbol=)` | Green success | Operation completed |
| `warning(msg, symbol=)` | Yellow warning | User action needed |
| `error(msg, symbol=)` | Red error | Operation failed |
| `verbose_detail(msg)` | Dim text, verbose-only | Internal details (paths, hashes) |
| `tree_item(msg)` | Green text, no symbol prefix | `└─` sub-items under a package |
| `package_inline_warning(msg)` | Yellow text, verbose-only | Per-package diagnostic hints |
| `dry_run_notice(msg)` | `[dry-run]` prefix | Dry-run explanation |
| `auth_step(step, success, detail)` | Auth resolution step | Verbose auth tracing |
| `render_summary()` | Render DiagnosticCollector | End of command |

### Subclass: `InstallLogger(CommandLogger)`

Install-specific phases. Commands that don't need these use `CommandLogger` directly.

| Method | Purpose | Output |
|--------|---------|--------|
| `validation_start(count)` | Start validation | `[*] Validating N package(s)...` |
| `validation_pass(name, present)` | Package OK | `[+] name` or `name (already in apm.yml)` |
| `validation_fail(name, reason)` | Package bad | `[x] name -- reason` |
| `resolution_start(count, lockfile)` | Start resolution | Context-aware install/update message |
| `download_complete(name, ref=, sha=, cached=)` | Package installed | `[+] name #tag @sha` or `(cached)` |
| `download_failed(name, error)` | Download error | `[x] name -- error` |
| `lockfile_entry(key, ref=, sha=)` | Lockfile verbose line | `key: locked at sha` / `pinned to ref` / omitted |
| `package_auth(source, token_type=)` | Auth source verbose | `Auth: source (type)` |
| `package_type_info(label)` | Package type verbose | `Package type: label` |
| `install_summary(apm, mcp, errors)` | Final summary | `Installed N APM dependencies.` |

### When to add a new logger method

If a command needs a new output pattern (new indentation level, new semantic meaning, new verbose gate), **add a method to CommandLogger or a subclass**. Signs you need a new method:

- You're writing `_rich_echo(f"    Something: {value}", color="dim")` in a command file
- You're checking `if logger.verbose:` before calling `_rich_echo` in a command
- You're formatting a string with specific indentation that other commands might reuse
- Multiple commands emit the same kind of line (e.g., file lists, auth info)

### Rule: No direct `_rich_*` in commands

Command functions must NOT call `_rich_info()`, `_rich_error()`, etc. directly. Use `logger.progress()`, `logger.error()`, etc. instead. The `_rich_*` helpers are **internal** to the logger and rendering layers.

**Exception:** Rich tables and panels for display (not lifecycle logging) may use `console.print()` directly — these are data presentation, not status reporting.

### Rule: Every command gets a `CommandLogger`

Every Click command function must instantiate a `CommandLogger` (or subclass) and pass it to helpers:

```python
@cli.command()
@click.option("--verbose", "-v", is_flag=True)
@click.option("--dry-run", is_flag=True)
def my_command(verbose, dry_run):
    logger = CommandLogger("my-command", verbose=verbose, dry_run=dry_run)
    logger.start("Starting operation...")
    _do_work(logger=logger)
    logger.render_summary()
```

### Rule: Verbose gating lives in the logger

Never check `if verbose:` in command code. Use methods that gate internally:

```python
# Bad — manual verbose check in command
if verbose:
    _rich_echo(f"    Auth: {source}", color="dim")

# Good — logger handles the gate
logger.package_auth(source, token_type)  # No-ops when not verbose
logger.verbose_detail(f"    Path: {path}")  # No-ops when not verbose
```

### DiagnosticCollector integration

Access via `logger.diagnostics` (lazy-initialized). The collector owns the collect-then-render lifecycle:

```python
# During operation — collect
diagnostics.skip(file, package=pkg_name)          # Collision
diagnostics.overwrite(file, package=pkg_name)     # Cross-package replacement
diagnostics.error(msg, package=pkg_name)          # Failure
diagnostics.auth(msg, package=pkg_name)           # Auth issue

# Query during operation (e.g., for inline verbose hints)
count = diagnostics.count_for_package(pkg_name, category="collision")
if count > 0:
    logger.package_inline_warning(f"    [!] {count} files skipped")

# After operation — render grouped summary
logger.render_summary()  # Delegates to diagnostics.render_summary()
```

### Visual hierarchy contract

Multi-package operations follow this tree structure:

```
  [+] package-name #v1.0 @b0cbd3df                    # download_complete
      Auth: git-credential-fill (oauth)                # package_auth (verbose)
      Package type: Skill (SKILL.md detected)          # package_type_info (verbose)
    └─ 3 skill(s) integrated -> .github/skills/        # tree_item
    └─ 1 prompt integrated -> .github/prompts/         # tree_item
      [!] 2 files skipped (local files exist)          # package_inline_warning (verbose)
  [+] another-package (cached)                         # download_complete

── Diagnostics ──                                      # render_summary
  [!] 2 files skipped -- local files exist             # Grouped by category
    Use 'apm install --force' to overwrite

[*] Installed 2 APM dependencies.                      # install_summary
```

Key rules:
- `[+]` package lines are the top-level anchors (green, no indent beyond 2-space)
- Verbose metadata (Auth, Package type) uses 4-space indent, dim color
- Tree items (`└─`) use 4-space indent, green color, no symbol prefix
- Inline warnings use 4-space indent, yellow color, verbose-only
- Diagnostics summary appears AFTER all packages, not inline (except verbose hints)

### Scaling guidance

As the CLI grows, this architecture scales by:
- **New commands**: Instantiate `CommandLogger`, use existing methods. Add subclass only if the command has distinct phases (like `InstallLogger`).
- **New output patterns**: Add methods to `CommandLogger`. Every command benefits.
- **New integrators**: Accept `diagnostics=` param, push to collector. No direct output.
- **Theme changes**: Modify rendering layer (`console.py`). Zero command changes.
- **Testing**: Mock `CommandLogger` in tests to assert semantic calls without parsing output strings.

## Anti-patterns

1. **Warning for non-actionable state** — If the user can't do anything about it, use `_rich_info` or defer to `--verbose`, not `_rich_warning`.

2. **Inline warnings in loops** — Use `DiagnosticCollector` to collect, then render a grouped summary after the loop.

3. **Missing `diagnostics` parameter** — When calling integrators, always pass `diagnostics=diagnostics` so warnings route to the deferred summary.

4. **No emojis, ever** — Emojis are completely banned from all CLI output. Use ASCII text symbols from `STATUS_SYMBOLS` exclusively. This applies to messages, help text, status indicators, and table titles.

5. **Inconsistent symbols** — Always use `STATUS_SYMBOLS` dict with `symbol=` param, not inline characters.

6. **Walls of text** — Use Rich tables for structured data, panels for grouped content. Break up long output with visual hierarchy (indentation, `└─` tree connectors).

7. **Direct `_rich_*` calls in commands** — Use `logger.start()`, `logger.progress()`, `logger.tree_item()` etc. The `_rich_*` helpers are internal to CommandLogger and console.py. Adding a `_rich_echo` call in a command file is a SoC violation.

8. **Manual `if verbose:` checks** — Use `logger.verbose_detail()`, `logger.package_auth()`, or other verbose-gated methods. The logger owns the gate.

9. **Manual `if dry_run:` checks** — Use `logger.should_execute` or `logger.dry_run_notice()`.

10. **Format strings for indentation in commands** — Don't write `f"    Auth: {source}"` in command code. Use `logger.package_auth(source)` which owns the indent level. When a new indentation pattern is needed, add a method to CommandLogger.

11. **Re-creating shared objects per iteration** — Expensive objects like `AuthResolver` should be created once before loops and reused per-package. The logger and diagnostics collector are already singletons per command invocation.

12. **Using `logger.progress()` for tree sub-items** — `progress()` adds a `[i]` symbol prefix. Tree continuation lines (`└─`) should use `logger.tree_item()` which renders with no symbol.
