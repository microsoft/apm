# #4889 design brief: keep a still-needed LOCAL dependency on uninstall

Status: STEP 1 outline (P2 lead with QL). Owners: P2 (install/uninstall), QL
(lifecycle cells, H8). The regent rules on the record from this brief.
Input: issue #4889, the INT draft record (Intake records/4889.md), and #4882
(the B-safe ruling for 0.38.0, which refuses today).

## 1. Problem in one paragraph

`apm uninstall ./vendor/e` refuses when another installed package still needs
`e` ("./vendor/e stays: ./vendor/d still needs it"). Remote dependencies get a
declaration-only removal (#4749). Local ones can't, because a local lock key
embeds where it was declared: `get_unique_key()` (`deps/lockfile.py:584`)
folds `declaring_parent` and `anchored_local_path` into the key. A direct
`./vendor/e` and the transitive `local:vendor/e` are therefore different
identities. Keeping the row unchanged breaks `apm install --frozen`, and the
next full install silently rewrites the row, its descendants and the
deployment-ledger owner ids.

## 2. Spec impact (OpenAPM v0.1)

Steps 2 and 3 verify each line against the spec.

- No lockfile format change (out of scope by record): re-keying uses existing
  fields only (`declaring_parent`, `anchored_local_path`, `local_path`).
- req-lk-006 (frozen install never writes the lock): the record's Done-when
  is that `--frozen` passes right after uninstall. Uninstall must therefore
  leave a lock that frozen accepts as-is. This is a consequence, not an
  amendment.
- req-lk-024 (delete only with deployment evidence): the core constraint.
  Re-keying must carry receipts and claim signatures to the new owner key, so
  later uninstall and prune still find evidence. A lost or forged binding
  must keep files, never delete them.
- req-lk-017 (frozen re-verifies entries): the re-keyed local row must verify
  against the same deployed-file hashes.
- req-rpl-003: parent values are "exact original lock keys". Check whether
  re-anchoring touches replacement `requests[].parent`; if so, those parents
  are re-keyed too or the case falls back to refusal.
- Candidate amendment (ruling needed): no req today states that a consumer
  MAY re-key a local entry when its declaring edge changes. Either (a) add a
  SHOULD under Section 7 local identity, or (b) treat it as an implementation
  detail covered by req-lk-006, req-lk-017 and req-lk-024 (no amendment).
  Recommend (b) for 0.38.x, (a) only if conformance tests need it.

## 3. Uninstall and prune deletion-evidence invariants

- INV-1: re-anchoring deletes no file. It only rewrites lock rows, ledger
  owners and receipt bindings.
- INV-2: after re-anchoring, every file the kept dependency deployed has
  exactly one owner (the new key) with an intact receipt binding (active
  owner plus claim signature), so req-lk-024 still holds.
- INV-3: a later uninstall or prune of the now-transitive `e` deletes only
  files APM proved it wrote. Edited files are kept with a named diagnostic.
- INV-4: any mismatch (unknown parent, receipt whose binding doesn't match,
  replacement parent not found, partial graph) falls back to today's refusal
  BEFORE any write (I7).
- INV-5: descendants' `declaring_parent` and `is_dev` are repaired graph-wide
  in the same commit, matching what `apm install --no-frozen` writes byte for
  byte.
- Owners to reuse, not duplicate: `deps/local_identity.py`
  (`transitive_local_identity`), `DeploymentLedgerCodec` legacy rebuild
  (`core/deployment_ledger.py`), receipt bindings in
  `DeploymentMaterializationSession`, and the survivor split in
  `commands/uninstall/cli.py`.

## 4. Migration of existing locks

- No format change, so there is no version bump. Locks written by 0.38.0
  (which refused) need no migration.
- Locks hand-edited or written before receipts existed (no binding):
  re-anchoring finds no evidence, so INV-4 refuses as today. No silent
  adoption.
- Prior partial states (a row kept with its old identity by a pre-fix probe):
  detect the stale key, then either re-key it under INV-1..INV-5 or refuse
  with a remediation naming `apm install --no-frozen`.

## 5. Done-when cells (from the INT draft, owned by QL)

1. Graph: local e -> f, dev d -> e, dev a -> f; `apm uninstall ./vendor/e`.
   Then `apm install --frozen` passes, and the lock is byte-identical to what
   a following `apm install --no-frozen` writes.
2. Later `apm uninstall` and `apm prune` of the transitive e keep deletion
   evidence and delete only proven files. An edited file is kept with a
   diagnostic.
3. A forged or mismatched receipt binding falls back to refusal before any
   write.
4. Each cell fails before the fix (today: refusal) and passes after.
   W3b ACCEPT at head; QL H8.

## 6. Open questions for steps 2 and 3

- Q1: Does any replacement `requests[].parent` reference a local key that
  re-anchoring changes? (Grep the req-rpl-003 writers.)
- Q2: Can the receipt re-binding reuse the #3772 adopt/rebind path
  (`adopt_root_receipts` plus the `_migrated` rebind after commit), or does
  it need a dedicated owner?
- Q3: Ruling between spec option (a) and (b) in Section 2.

## Steps (rule 22: hourly, each ends in a pushed SHA)

- STEP 1 (this commit): outline.
- STEP 2: verify Section 2 against the spec text and answer Q1/Q2 with code
  citations.
- STEP 3: finalize invariants and cells with QL; hand the brief to the
  regent for ruling.
