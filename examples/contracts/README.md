# First local contracts

These are authored, secret-free fixtures, not copies of a governed project.
Copy this directory to a fresh disposable directory outside another Git
repository. Use an installed APM build containing Contracts v0.1, Python 3,
and an authenticated native Copilot CLI with access to your selected model.
Do not remove a project's remotes or policy to make it eligible.

## Produce and assess a handoff

From the copied `first-contract/` directory:

```sh
apm experimental enable contracts
apm plan ./handoff.contract.md --on copilot --model gpt-6-astra
apm run ./handoff.contract.md --on copilot --model gpt-6-astra --allow-advisory
```

The model is an explicit demonstration selection, not an APM default.
Planning does not call a model, install packages or execute checks.
`--allow-advisory` is required in terminals and pipes, with no prompt or
remembered consent. Native processes use your host identity: this is not
filesystem/network isolation or a hard spending cap.

Inspect the artifact and record paths printed by APM. The captured handoff
lives under `.apm/runs/<run-id>/`, not over an existing `handoff.json` in your
project. The standard-library-only checker assesses JSON shape and source-ID
coverage, not the complete factual correctness or quality of the prose.

## Reuse an installed skill

The second fixture shares the first fixture's source notes and parameterized
checker. From the copied examples directory, prepare its explicit resources:

```sh
mkdir -p reuse-contract/checks
cp first-contract/notes.md reuse-contract/notes.md
cp first-contract/checks/check_handoff.py reuse-contract/checks/check_handoff.py
cd reuse-contract
apm install --only apm --target copilot
apm experimental enable contracts
apm plan ./handoff.contract.md --on copilot --model gpt-6-astra
apm run ./handoff.contract.md --on copilot --model gpt-6-astra --allow-advisory
```

Installation is a separate, explicit action. The contract names the declared
`handoff-style` skill, not an `apm_modules/` path. APM supplies its selected
content without invoking another agent or granting tools. The extra check
assesses its caution format. Local lock identity plus observed source bytes
does not establish a cryptographic pin or protected provenance.

## Read outcomes literally

| Outcome | Meaning in this slice |
| --- | --- |
| VERIFIED / 0 | Captured output passed every required check under accepted native-advisory controls. |
| REJECTED / 20 | A check returned a failed condition, even if another check was incomplete. |
| UNPROVEN / 21 | Output or assessment is missing/incomplete, or required consent/assurance is unavailable. |
| HALTED / 22 | Execution, cancellation, watchdog, capture or recording stopped the invocation. |

Raw check exits are retained: 0 passes, 1 fails, 2 is incomplete; unknown exits,
missing tools and signals are incomplete. No output does not mean `no_change`.
Every check gets a fresh baseline and the captured file. Patch checks apply
their own patch; APM does not apply it first.

The first profile supports macOS/Linux and positively established no-policy
projects. Governed/unresolved-policy projects, Windows execution, command
leaves, `budget`, `sandbox`, captures, output alternatives and composed jobs
refuse before inference. Passing checks never authorizes merge or delivery.
