---
applyTo: "**"
description: "P8 shipping requirement: prove features across the applicable command state machine, not just installation."
---

# Lifecycle completeness

[PRINCIPLES.md P8](../../PRINCIPLES.md#p8----lifecycle-completeness)
is mandatory for developers, coding agents, reviewers, and completion
drivers. Read this rule before planning, implementing, reviewing, or
certifying a change. It is the single operational lifecycle rule;
specialist prompts reference it rather than keeping narrower checklists.

## Establish the whole contract before editing

For every feature, fix, or lifecycle-affecting refactor:

1. Derive the command/subcommand inventory from the executable CLI at
   the candidate revision. Do not use a remembered list or documentation
   headings as the authority.
2. Map the changed feature and its dimensions to every command. Include
   observers, no-ops, safe refusal, and failure/recovery, not only commands
   that write files. Explain genuinely unrelated commands specifically.
   "Install-only change", "read-only", "no fixture", "not modeled", or an
   unsupported flag is not an applicability exemption.
3. Plan a connected real-CLI trajectory through all applicable transitions
   in one isolated workspace. Reuse the existing APMLifecycle runner,
   local repositories, state snapshots, and physical artifact snapshots.
   Preserve state between commands; do not reset the project to make each
   command pass independently.
4. Integrate relevant dimensions and changed interactions into the
   existing generated state-machine model. Require deterministic replay
   of mandatory transitions as well as bounded generated exploration.
   A parameter or rule that never executes is not coverage.

Feature dimensions include scope, path spelling, source/ref type, target,
primitive shape, ownership, policy and authentication where relevant.
Installed markdown payloads, lockfile-only changes, and shared helpers
can affect lifecycle behavior. Do not classify them by filename alone.
Purely unrelated contributor prose can be not applicable; uncertainty
must be resolved, not silently classified away.

## Prove transitions, not success messages

The trajectory must exercise relevant install, reinstall/idempotency,
version publication and update, lock/frozen replay, compile, audit,
outdated, removal, and reinstall-after-removal behavior. This is a
planning spine, not a substitute for the complete command map or a
license to invent unsupported command flags.

At command boundaries, assert the contract on real artifacts: deployed
bytes and layout, lock and ownership consistency, permitted write sets,
and survival of unrelated user files. For observers and previews,
compare before/after durable state. Use independent physical-root
snapshots as well as ownership-derived snapshots; a missing ownership
record must not hide collateral writes.

For failures, inject a deterministic fault, assert the documented
allowed/forbidden effects, restore the precondition, then retry in the
same workspace. Do not assume every operation is atomic. Input aliases
remain aliases while observations use the physical roots; never relax
containment checks to make a scenario pass.

Use hermetic fixtures without personal credentials or live services.
Distinguish installed Python CLI evidence from packaged-binary evidence.
Run the required platform/capability lane when a feature depends on it;
a skipped required case is not a pass.

For a bug fix, remove the relevant production correction and show the
lifecycle regression fails for the intended reason. Restore it and
rerun the clean candidate. Unit or component tests remain useful, but
their totals cannot substitute for lifecycle assertions.

## Execute the shipping gate

The deterministic entrypoint is
`scripts/check_lifecycle_evidence.py`, backed by the existing lifecycle
ledger and test machinery. Read its `--help` and the contributor testing
guide before first use. A missing entrypoint or rule is a blocker, not
permission to invent an evidence document.

At the final integrated revision, execute:

```sh
uv run --extra dev python scripts/check_lifecycle_evidence.py \
  --base <comparison-base-sha> --head <candidate-sha> \
  --lane full --report <session-artifact-path>
```

Run from a clean committed candidate checkout; write reports outside it.
The gate executes verification; do not substitute imported, hand-written
or earlier-head reports. Use its computed applicability, collection,
execution and candidate identity. The bounded `pr` lane is preliminary:
pending generated-model evidence cannot certify shipping. Required
merge-candidate execution is distinct from local PR-head evidence.

Missing cases, deselection, skips, xfail, failures, stale evidence, and
unexercised required variants/transitions block completion. A lifecycle
gap remains in the feature's scope even if the original brief omitted
it. Re-plan or return blocked; do not defer it to a follow-up issue.
Existing adequate tests may satisfy a refactor without being rewritten.

## Review and completion

The coverage reviewer audits the command map, both lifecycle layers,
feature activation, state assertions, and actual execution identity.
Reviewers must not infer a pass from filenames, source inspection,
exit-code-only assertions, or the author's narrative.

Neither `ready-to-merge` nor `advisory-with-deferred` is valid while
required lifecycle evidence is missing. An advisory panel cannot waive
P8. The driver executes the gate at its final head; its parent
independently executes it before accepting terminal completion.
The parent uses `--completion <return-json>` with a fresh report path to check the
driver's summary and digest while independently re-executing the full gate.
Integrating main, rebasing, or changing code/tests invalidates older
evidence. Keep applicability and fixture limitations explicit; bounded
execution does not prove every possible permutation.
