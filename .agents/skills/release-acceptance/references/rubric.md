# Severity, cut call and routing

## Severity

| Severity | Use when | Examples from 0.37 |
|---|---|---|
| blocker | A common journey step fails or misleads; data loss; an executable runs without a current approval; a release headline promise is broken | audit tells you to run `apm install` to repair drift and the install does not repair it; a valid dependency deploys when a sibling dependency fails |
| should-fix | A wrong hint, stream or exit code; one sibling cell disagrees with the others; docs and CLI disagree on a common path | `update` suggests `--no-policy`, which update does not accept; `--exclude VSCode` ignored while `--target VSCode` works |
| nit | Wording, counts, pluralization, duplicate lines, formatting | "1 agents integrated"; "#main #main" |
| blocker (pre-existing, security) | An approval, policy, integrity or credential defect that also reproduces on the previous release | a denied executable stays registered after reinstall on both N-1 and the candidate |

A NON-SECURITY finding that already reproduces on the previous release
is PRE-EXISTING. It is a known issue for the release notes, not a cut
blocker, unless the release claims to fix it.

A trust, integrity or credential finding (lens L4, or any approval,
policy, integrity or credential issue) is never a release-notes known
issue, even when it reproduces on the previous release. Security
findings get fixed, not reported: it routes privately to the security
lead with a failing-before test brief and is fixed before the tag. No
advisory is published instead of a fix.

## Cut call (the release-gate row)

`scripts/findings.py gate-row` computes it:

- GO: no blocker is open, verified, still-open or changed at the exact
  SHA.
- A pre-existing security finding counts as an open blocker until it is
  fixed; a pre-existing non-security one does not.
- NO-GO: at least one such blocker, listed by id.

The row also lists blockers still awaiting verification and every
verified finding that has no failing-before lifecycle test yet. A human
approves the row (B10) before it is published to the release gate.

## Routing

1. Owner family: read the owner registry under `.apm/architecture/owners/`
   and pick the owner whose module holds the root cause file. Families
   used so far: identity/selector, lifecycle plan, vocabulary, trust,
   lock writer, target resolution, marketplace origin, MCP targets,
   pipeline apply barrier, other(<name>).
2. Target: the open release-train PR that touches that owner, or "new".
3. Security and trust findings (lens L4, or any credential, approval or
   integrity issue) go privately to the security lead and the
   coordinator who started the pass, never into public issue or PR
   text. Public wording stays neutral.
4. Every verified finding gets a failing-before test brief for the
   apm-lifecycle-tests skill (see SKILL.md P5). The finding stays open
   until that test exists and fails on the unfixed code.

Never file issues, comment on PRs or push. Routing is a message to the
human or coordinator who asked for the pass.
