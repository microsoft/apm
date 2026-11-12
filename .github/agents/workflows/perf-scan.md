# perf-scan -- Agent Instructions Reference

Companion instructions for `.github/workflows/perf-scan.md`.
This file serves as a standalone reference for the agent prompt used by the
Daily Performance Scanner agentic workflow.

---

## Purpose

The Daily Performance Scanner runs every day at 01:00 UTC and scans
`src/apm_cli/` for six algorithmic performance anti-patterns. It creates
a GitHub Issue on every run so the team has a daily record.

## Agent persona

The agent operates as the `performance-expert` persona defined in
`.github/agents/performance-expert.agent.md`. When scanning non-transport
code (i.e., anything outside `src/apm_cli/transport/`), it also loads the
pattern catalogue from `.github/agents/algorithmic-patterns.agent.md`.

## Anti-patterns checked

| ID | Name | Typical Big-O impact |
|----|------|----------------------|
| A | Quadratic loop nesting | O(n) -> O(n^2) as package count grows |
| B | Linear scan in loop (`x in list`) | O(1) -> O(n) per iteration with a set/dict |
| C | Unconditional expensive ops on hot paths | Avoidable I/O / serialisation per call |
| D | Redundant env-var / config parsing | Repeated syscalls or file reads per invocation |
| E | Heavy top-level imports in command modules | Startup latency added regardless of sub-command |
| F | Sequential independent I/O (no data dependency) | Wall-time vs. parallelism opportunity |

## Scan scope

- **Included**: `src/apm_cli/**/*.py`
- **Excluded**: `tests/`, generated files, vendored code

## Output contract

Every run creates exactly one GitHub Issue:
- Found findings: `[perf-scan] YYYY-MM-DD -- performance opportunities found`
- No findings: `[perf-scan] YYYY-MM-DD -- no issues found`

Labels applied: `type/performance`, `type/automation`.

The issue body is ASCII-only and uses the format documented in
`.github/workflows/perf-scan.md` Step 5.

## Finding format

Each confirmed finding includes:

```
#### [{pattern_id}] {Pattern Name} -- {file}:{start_line}-{end_line}

- Current: {current complexity or cost}
- Proposed: {proposed complexity or approach}
- Fix: {one concrete sentence}
```

## Exit conditions

The workflow always creates an issue (even when no findings are present).
This ensures the daily run is always visible in the issue tracker.

## Related files

- Workflow definition: `.github/workflows/perf-scan.md`
- Performance agent: `.github/agents/performance-expert.agent.md`
- Pattern catalogue: `.github/agents/algorithmic-patterns.agent.md`
- Benchmark examples: `tests/benchmarks/test_perf_benchmarks.py`
