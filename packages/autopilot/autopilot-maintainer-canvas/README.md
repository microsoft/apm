# Autopilot maintainer canvas

Copilot App control surface for microsoft/apm CODEOWNERS. Live GitHub
is the only queue. This is not the contributor dashboard.

The canvas can apply `status/accepted`, `status/deferred`, and
`panel-review`. It never assigns, never requests reviewers, never
merges, and never runs scheduler or worker logic in the canvas
session. Actions spawn the canonical isolated autopilot skills
as activation cards (not prose kickoffs). Merge-worker spawn uses
`kickoff_mode: plan` (`plan_first: yes`); other skills stay
interactive.
Agentic Workflows must not open this canvas. Clicks show a Spawning
or Working banner immediately; working sweep buttons stay disabled.
An Active sessions table lists scheduler and worker sessions with
Role, Session, Status, and Open. Child workers are indented under
their scheduler. Open uses the Copilot App session URL. It does
not detach. Refresh status
(or expand the bar) reads the local Copilot App session catalog; it
does not wait on a chat turn.
The table
splits human status from triage: Not triaged vs Triaged plus the
comment conclusion (accept, needs-design, needs-issue, and the rest).

## Install

```bash
apm experimental enable canvas
apm install packages/autopilot/autopilot-maintainer-canvas --target copilot
```

Then in Copilot App: "Open the Autopilot maintainer canvas".

The canvas id is `autopilot-maintainer`. Deployed
`.github/extensions/` is generated. Edit
`.apm/extensions/autopilot-maintainer/` in this package.

## Tests

```bash
node --test tests/*.test.mjs
```
