# Design brief: persisted APM-written MCP/hook state (#4784, #4864, #4867; context #4899)

Owner: P3 lead (9a22ec4b) with QL. Rule 22: hourly steps, each a pushed SHA.
Status: STEP 4 FINAL for Regent (Mon 10:15), QL-reviewed. Steps: S1 outline (09:40) -> S2 options A/B/C/P
with mechanism and call sites (10:40) -> S3 risks + OpenAPM/compat cells per option (11:40)
-> S4 QL review folded + recommendation + decision memo for A/B (12:40).
Any option that changes MCP retirement behavior goes to the Regent as a decision memo.

## 1. Shared question
Which records of state that APM itself wrote (per runtime, config file and native key)
does APM persist, and when may APM act on them (retire, keep, or delete a file)?
Invariant for every option: APM never vouches for bytes it did not write, and never
removes an entry or file it cannot prove it wrote. Where proof is ambiguous, keep the
entry and report it.

## 2. The three asks
| Issue | Trigger | What is missing today |
|---|---|---|
| #4784 | No lockfile; a server stays declared; a runtime is no longer targeted | Retirement skips by server NAME (write_receipts.py ~341), so the untargeted runtime's entry stays and `audit --ci` passes silently |
| #4864 | Lockfile present; a retirement was refused (kept) and the targets change | No per-runtime declaration baseline, so a newly targeted runtime could be vouched for with an old baseline |
| #4867 | APM empties a native MCP/hook config file | No file-creation provenance, so a user-created (empty) skeleton could be deleted when APM empties it |
| #4899 (context) | Refused-retirement baseline native-key mismatch | Narrow fix via existing fields (Intake record); any new per-runtime field is decided here |

## 3. Evidence
- ENG-B factual note (current behavior, keying options, 7 risks):
  session cda0c81a files/4784-design-note.md
- QL strict cell (XFAIL on CI 45079492557):
  tests/lifecycle/scenarios/test_mcp_write_receipts.py::
  test_an_apm_written_entry_for_a_runtime_no_longer_targeted_is_retired_without_the_lockfile
  (done-when: cursor entry retired, OR kept AND reported)
- INT draft scopes: session 982e07e9 files/records/4864.md, records/4867.md

## 4. Options: mechanism and call sites (STEP 2)
Shared anchor: `McpWriteReceipts.retire_undeclared` (install/mcp/write_receipts.py:329).
Today it skips a record when `write.name in declared` (line 347), so a still-declared
server is never retired from a runtime the project dropped. Records are already keyed
(runtime, config, native key) (#4818); the gap is the NAME-level skip, not the key.

### C. Report only (no behavior change to removal)
- Mechanism: in `retire_undeclared`, a record whose server is declared but whose
  runtime is outside this run's deployed runtimes goes to `mark_kept` + `warn_kept`
  ("Kept MCP server '<name>' in <config>: APM no longer targets <runtime>").
- Needs: the set of runtimes deployed this run, from the install journal
  (`note_mcp_write`, line 118) passed by `retire_undeclared_mcp_writes` (line 435).
- `audit --ci` already fails on kept-mcp-entries, so the "passes silently" half closes.
- Schema: none. Spec: none (reporting only). Satisfies QL cell arm "kept AND reported".

### A. Retire by deployed destination, with an explicit skip set
- Mechanism: replace the NAME skip with a DESTINATION skip: keep the record iff
  (runtime, config, key) was deployed this run OR its runtime is in `skipped`
  (--exclude, unavailable/undetected runtime, failed write). Otherwise, if proven,
  `_remove_native_entry` as for undeclared servers.
- Needs: journal of deployed destinations + a skip-reason set produced by the MCP
  integrator (mcp_integrator_install); retirement only on full-project installs
  (not `apm install <pkg>`, --only); keep `_selected` and `_declared_keys` guards.
- Schema: none if the skip set is per run; behavior change = removal, so Regent memo.

### B. Persistent per-runtime target baseline (shared with #4864)
- Mechanism: the lockfile (or receipt store) records, per runtime, the declaration
  baseline APM last wrote (additive field). Retire only for runtimes outside the
  project's resolved target set, never on per-run skips; a refused retirement keeps
  its old per-runtime baseline (#4864) so a newly targeted runtime is judged on its own.
- Needs: lockfile field + reader/writer round-trip; OpenAPM lockfile reference update.
- Schema: additive lockfile field (old readers ignore it). Behavior change = removal,
  so Regent memo; one schema change should serve #4784 and #4864 together.

### P. File-creation provenance (#4867, orthogonal)
- Mechanism: when APM creates a native MCP/hook config file, the receipt store records
  "created by APM" for that file; deleting a file APM emptied requires that record.
  Files without the record keep today's content-proof behavior until APM next creates them.
- Call sites: the emptied-file delete paths (MCP config writers; hook_integrator cleanup).
- Schema: additive receipt field. Tightens deletion only (safety-positive): no memo.

### Overlap
- #4864 == B's baseline (same field); #4784 can ship as C now and A or B later.
- #4899 stays narrow (existing fields) per its record; it must not pre-empt B's field.

## 5. Risks per option (STEP 3; ENG-B risks R1-R7, QL to confirm)
R1 transient vs durable skip; R2 auto-detected targets; R3 shared destinations;
R4 partial installs; R5 COPILOT_HOME/user profiles; R6 user-held identical entry;
R7 test surface.

| Risk | C report | A destination+skip set | B persistent baseline | P provenance |
|---|---|---|---|---|
| R1 | none (keeps) | HIGH: an unlisted skip reason removes a working entry; needs a closed skip-reason enum and retire only after a successful deploy pass | low: per-run skips never retire | none |
| R2 | none | HIGH: no explicit targets means the machine decides | MED: baseline must record explicit vs detected targets; retire only on explicit | none |
| R3 | none | MED: keep `_declared_keys` per (config, key) and req-mf-027 file attribution | MED: same | MED: a file shared by two targets is created once; provenance per file, not per target |
| R4 | none | HIGH unless gated to full-project installs | low | none |
| R5 | none | keep `_selected` guard | baseline keyed by selected config | provenance keyed by resolved file |
| R6 | keep+report stays | proof = fingerprint; ambiguous keeps | same | n/a (file-level) |
| R7 | 1 cell | 5+ cells (retarget, --exclude, unavailable, shared, partial) | 4 cells + round-trip | 3 cells (#4867 record) |
| Deletes anything new? | no | yes (entries) | yes (entries) | no (only blocks file deletes) |

## 6. OpenAPM / compat per option (STEP 3)
- C: no spec change; audit kept-mcp-entries already specified behavior. Cell: QL strict
  cell passes on the "kept AND reported" arm; no compat cell.
- A: retirement of a declared server's entry is new normative behavior: needs a
  spec-citation fold (new consumer MUST under Section 4.5 next to req-mf-026/027) or
  the Mode B gate fails. No lockfile change; compat = none.
- B: additive lockfile field (per-runtime declaration baseline). Cells: old lockfile
  loads and round-trips unchanged (req-lk-011 omission rules; req-lk-005 no-op
  stability: a no-op install must not add the field), older reader tolerates the
  field (lifecycle cell, #4864 record), lockfile reference page + CHANGELOG. Spec fold
  for the field and its retirement rule.
- P: additive receipt-store field (receipts are local state, not the lockfile), so no
  lockfile schema change; deletion stays under req-lk-024 evidence (tighter). Cells
  per the #4867 record: user-created empty file kept, APM-created file removed,
  pre-provenance install unchanged. Spec: optional note under req-lk-024; no new MUST.

## 7. Recommendation (STEP 4; QL AGREES, inbox 148, conditions Q1-Q7 folded)
### 0.38.x (no Regent memo needed: report-only or tighter deletion)
- #4784 = C. Q1: kept-and-reported PERSISTS: `mark_kept` writes the record, so every
  later install and `audit --ci` (kept-mcp-entries) report it until resolved, not a one-time
  warning. Acceptance: the QL strict cell passes on its "kept AND reported" arm and its
  Known entry flips in the same PR (H4).
- #4867 = P, as the SINGLE owner of "may APM delete a config file it emptied".
  Q2: today #4862 (main, 09:2x) proves "APM created it" by CONTENT SHAPE only:
  `hook_file_emptied` (integration/hook_ownership.py:665, file holds only the target's
  injected defaults, no symlink below the root) and `_write_cleaned_json_config(...,
  delete_emptied)` (mcp_integrator.py, file holds only the servers key, emptied by this
  cleanup). P replaces both with one owner; no second authority stays beside it.
  Q3: a provenance record alone never authorizes a delete. Delete only when ALL hold:
  (a) the receipt says APM created the file; (b) its bytes equal the bytes APM last wrote
  or APM's empty skeleton (fingerprint); (c) the receipt store is local state outside any
  VCS-tracked path, so a clone never ships a "created by APM" record. Else keep and report.
  Q4: provenance keyed by the resolved file, one red-first cell per class: path with
  whitespace/encoding (COPILOT_HOME with spaces); missing file falling back to a parent;
  config file as a symlink to outside (never touch the target) and as a FIFO; case and
  Unicode (case-insensitive FS, NFC/NFD profile dir); traversal in config/profile paths.
- #4899 stays narrow on existing fields (Q7).
### 0.39 (Regent memo required: both remove entries)
- #4784 retirement = A + B decided together with #4864 (one schema change).
  Q5 (A): closed skip-reason enum; retire only after a successful deploy pass on a
  full-project install (not `install <pkg>`, --only, --dry-run); keep `_selected` and
  `_declared_keys`; an unknown reason keeps.
  Q6 (B): the baseline never authorizes removal alone; each retirement still needs the
  per-entry write proof (fingerprint, R6), so an edited committed baseline can only make
  APM keep, never remove a user-held entry. Field omitted when unchanged (req-lk-005).
- Oracle: a twin of the QL cell for the "retired" arm, Known-red until A/B land.

## 8. Decision memo to the Regent (A/B only)
Ask: approve A+B (with #4864) for 0.39 under Q5/Q6, or keep C as the permanent rule.
Recommended default: approve A+B for 0.39; C ships in 0.38.x either way.
Why: C closes the silent-audit gap with no removal; A+B are the only way to actually
retire entries for dropped runtimes, and Q5/Q6 bound them to proven, APM-written
entries on full installs. Cost: 1 additive lockfile field, a spec fold (Section 4.5),
about 9 lifecycle cells. Risk if declined: kept entries accumulate and stay reported.
