# Report template

Write `REPORT.md` in the run directory with exactly these sections. Fill
every angle-bracket field from script output, not from memory.

```markdown
# Release acceptance: <version> at <sha>

Mode: <full | recheck>. Run directory: <path>. Catalogue: <source path>
(<embedded: false>). Atlas: <used | unavailable>.

## Release-gate row

| Gate | SHA | Verdict | Open blockers | Unverified blockers | Verified without lifecycle test |
|---|---|---|---|---|---|
| exploratory acceptance: no open verified blocker on the exact SHA | <sha> | <GO or NO-GO> | <ids or none> | <ids or none> | <ids or none> |

Approved by: <human or coordinator, after the B10 checkpoint>.

## Findings

| Id | Severity | Step | Cell | Title | Owner family | Route | Status | Lifecycle test |
|---|---|---|---|---|---|---|---|---|
| RA-001 | <sev> | <J.n> | <tuple> | <title> | <family> | <PR or new or private> | <status> | <test node or pending> |

(Security findings: title "private finding RA-NNN", details sent privately.)

## Recheck (recheck mode only)

| Id | Verdict at <sha> | Evidence |
|---|---|---|

## Coverage

From `scripts/findings.py coverage`: probed ratio <x>; per journey:
<journey: pass / fail / not-covered / unreachable>.

## Gaps

<what was not covered and why; model or host incidents>
```

## Finding record fields

title, severity, step, cell, observed, expected, repro, sha, owner_file,
owner_family, consistency_class, lens, status, lifecycle_test, id,
fingerprint, history (added by recheck).
