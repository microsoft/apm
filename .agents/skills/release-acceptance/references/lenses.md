# Lens overlays

A lens changes WHAT a walker looks for while it walks a program journey.
The journey (commands, expected exits, must-print lines, files) comes
from the program catalogue. Lenses only add checks; they never add a
journey copy.

Each walker gets ONE journey and ONE lens. Pick lenses per journey from
the table, ranked by the risk plan (P2 in SKILL.md).

## Lens cards

| Lens | Persona | Adds these checks | Depth persona |
|---|---|---|---|
| L1 hint truth | First-week user | Execute every hint and docs link the CLI prints; the hint must reach the stated outcome. Copy commands exactly as printed, including wrapped ones. | devx-ux-expert |
| L2 output contract | Docs-first explorer | Every line: ASCII, no duplicates, no stale names, errors on stderr, documented exit code, counts match reality, nothing contradicts. Compare with `--help` and the reference page. | cli-logging-expert |
| L3 consistency | Consistency auditor | Repeat the step in sibling cells: scope (project, user), mode (`--frozen`, `--dry-run`, `--json`, offline, cold or warm cache), target (every harness in the run), and command (install, update, uninstall, prune, audit, pack). The same decision must hold in every cell. | python-architect |
| L4 trust and integrity | Approvals reviewer | Every step runs with policy ON (`--no-policy` only when the step is about that flag). Executables only run with a current approval; deny retires; policy warn then block; lock or catalogue edits are detected; no credential appears in output, lock or logs. Route findings privately. | supply-chain-security-expert |
| L5 environment | Platform engineer | Relocated APM_HOME and harness homes, CRLF, case-insensitive file system, long paths, proxy-only and offline, private hosts (local stand-ins). | auth-expert |
| L6 upgrade and team | Second teammate | Prior-release project and lock upgraded to the candidate; a teammate pulls a lock bump, switches branches, resolves a lock conflict; a mixed-version team. | test-coverage-expert |

Persona names are wording for the brief. The depth persona is the
existing agent whose file the walker may read for detail.

## Journey to lens map (defaults; the risk plan can add more)

| Journeys | Lenses |
|---|---|
| J01 J02 J03 (golden consumer) | L1 L2 L3 |
| J04 J05 J06 J07 J08 J09 | L1 L3 |
| J10 (user scope) | L3 L5 |
| J11 J12 (bundle, drift) | L1 L3 L4 |
| J13 J14 J15 | L1 L2 |
| P01 P02 P03 P04 (producer) | L1 L2 L4 |
| E01 E02 (policy, executables) | L1 L4 |
| E03 E04 (proxy, private hosts) | L5 |
| E05 E06 (audit evidence, registry) | L2 L4 |
| any journey, prior-release fixture | L6 |

## Flagship chain

Run once per full pass, as one world, in this order:
P03 publish to a marketplace, then E01 org policy pinned to it, then
J05 consume by name, then P01 release a new version, then J07 update
across the consuming projects, then J03 CI gate, then E05 inventory.
Lenses L1 and L3 apply throughout.
