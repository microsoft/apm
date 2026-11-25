# Section 3: Security invariants for the v0.40 architecture (consolidated)

Sources:
- `w3d:ruling:containment-once` (K1-K9);
- `w3d:ruling:integrate-2.1` (IR1-IR9);
- `w3d:ruling:materialize-once` (MS1-MS7);
- `w3d:ruling:manifest-deploy` (MD1-MD5).

Shapes:
- **A**: plan once per package (classify, inventory, parse once).
- **B**: bulk skill deploy.
- **C**: containment by construction.
- **M2**: materialize once (fetch into the final tree, cache = objects).
- **MD**: manifest-driven deploy.

The threat model, unchanged from containment-once:
- **IN model:** links committed in the user's repo; package user code (lifecycle scripts, hooks) mid-command; trees persisted from earlier runs and changed between runs; Windows junction, 8.3 and case aliases; APM's own worker threads sharing proof memos (`w3d:5382:train-T1T2`).
- **OUT of model:** a concurrent same-user process racing between proof and open.

## 3.1 Invariant table

| id | guards | binds | proving cell (K9/differential unless noted) | cost |
|---|---|---|---|---|
| **MS1** one trust root | every byte in `apm_modules` is bound to the lock pin (commit + content_hash), by re-hash-as-written (one-use, same command, dev/ino) or by verify at use; never "it came from the cache" | M2, MD | tampered cache object refused; user edit in `apm_modules` caught by `verify_locked_package` | 0 (hash at inflate) |
| **MD1** manifest hash == pinned tree blob id | the manifest is not a trust root | MD | manifest entry whose bytes differ from the tree's blob id -> refused / git | 0 |
| **MS6** objects re-hashed on read | cache tamper never yields a silent byte | M2, MD (warm re-derive) | tampered/missing object -> refetch or git | 1 hash per object read |
| **MS5** fallback parity | anything not provably equal to git's checkout (attributes, eol, sparse, promisor, LFS, submodules, tracked links; C1-C12) goes to git in a stage, then the same land + verify | M2, MD | each C1-C12 trigger -> git path, identical tree | 0 on the fast path |
| **MD5** completeness | a package with any unprovable entry (git fallback, carried/warm tree, link, mode mismatch, extra entry) takes today's walk/hash/classify for the WHOLE package | MD | extra on-disk entry -> whole package to today's path | 1 set compare per package |
| **MS2** isolation | no mutable inode shared between the cache and any project tree; hardlinks REJECTED (M3) | M2 (and M1 reflink) | eviction/refetch in project A never changes project B's `apm_modules` | 0 |
| **MS3** write discipline | exclusive stage, per regular file O_EXCL\|O_NOFOLLOW / CREATE_NEW, never a directory clone/copytree, C8 seal, atomic land, #5410 race contract | M2, B | link planted in the stage -> seal refuses | 1 lstat per staged entry |
| **MS4** metadata | mode only from the tree entry, set explicitly; no owner/ACL/xattr carried from the cache | M2 (M1 clone) | mode differential vs today (0644/0755) | 1 fchmod per file |
| **MS7** proof | lockfile, receipts and deploy store unchanged; `apm_modules` bytes AND modes identical | M2, MD | L/W/M differential incl. modes | test only |
| **K1 / IR2 / MD2** proof lifetime | every proof (dir/root proof, plan link-free set, manifest landing proof) lives in `invocation_memo`; dropped by `forget_invocation_memos` (user code) and by `release_held` clear-all (any APM rename/delete/link) | C, A, MD | lifecycle script plants a link between plan and deploy -> today's refusal | 0 |
| **T1/T2** publish race (`w3d:5382:train-T1T2`) | a proof is published only if no forget ran since its observation (generation under lock) | C, A, MD | 3 race cells: in-thread, the `_proven_answer` site, cross-thread (Event-parked) | 1 lock per publish |
| **K2 / IR1** by-construction scope | "proven by construction" ONLY for trees whose every entry APM wrote via no-follow exclusive creates (in-process landing with C8 seal, APM stages). A git checkout, or a cache copy made without refusing links, takes the I2 walk | C, M2 | git-fallback checkout holding a tracked symlink -> walked and refused as today | 0 / 1 walk per root |
| **K3 / IR3** read side | every source READ is no-follow plus `fstat` S_ISREG plus dev/ino match; a proof removes the resolve, never the leaf check | C, A, B, MD | leaf swapped for a link after the plan walk -> refused | 1 open + fstat (already paid) |
| **K4** new dir components | proven (exclusive mkdir) before first use | C, B | new intermediate dir planted as a link -> refused | 0 |
| **K5 / IR4** leaf writes | new leaf O_CREAT\|O_EXCL (Windows CREATE_NEW); existing leaf lstat-refuse link/ANY reparse point, then atomic replace; never write in place; on nt every link test refuses any reparse point (junctions fail S_ISLNK) | C, B | hardlinked existing leaf -> target untouched; `.claude` junction (W) -> refused | 1 lstat per replace |
| **IR5** lexical names | every component validated before a lexical join: `..`, ADS `:`, trailing dot/space, device names, 8.3 `~N`; one validator (`_safe_name`/`validate_path_segments`) | C, A, MD | each hostile component -> refused | string checks |
| **K6 / IR7** relpath parity | a lexical rel only when the proof saw no in-root link, computed against the ONE resolved root | C | in-root link, case-variant root, 8.3 root (`RUNNER~1`) -> lockfile and receipts identical | 0 |
| **K7** memo key | exact lexical text, no case folding; an alias gets its own proof | C | case alias -> separate proof | 0 |
| **IR6** Shape B stage | stage built per regular file from the plan's link-free set (reflink per file OK), C8-sealed; an existing dir moved aside through the journal, only when absent or ledger-owned in full | B | stage seal; non-empty foreign dir -> refused | 1 lstat per staged entry |
| **IR8** outcome parity | a link or out-of-root entry keeps today's per-file outcome and diagnostic; the gate scan still reads every deployed byte | A, C | planted link -> the same message as `_is_safe_source_path` | 0 |
| **MD3** receipts | deployed-file receipts and lockfile hashes are computed over the bytes WRITTEN to each target in the same pass; a transformed render never inherits a source hash | A, B, MD | render differential (receipts byte-identical) | 0 extra reads |
| **MD4 / K8 / IR9** single owners | one classifier (fed the manifest path set, content-reading classes keep reading); proof recording only in the containment owner + plan owner; arch guard + MutationCase | A, C, MD | class(manifest paths) == today's classification on L/W/M; guard mutation cases | test only |
| **K9** differential | today's verdict, lockfile and receipts under planted links (dir/intermediate/leaf), W junction/8.3, case alias, in-root link, mid-install script, warm planted leaf, hardlinked leaf, old `apm_modules` link, git checkout with a symlink | all | `perf-research/arch-render-diff` + a K9 cell set | test only |

## 3.2 Conflicts and gaps between rulings: RESOLVED

W3d ruled them in row `w3d:ruling:v040-gaps`; the wording below is W3d's.

- **G1 (K2 vs IR1): RESOLVED.**
  - IR1 governs. K2 "by construction" means every entry was created by APM with an exclusive, no-follow, per-file create in this invocation.
  - That covers: the lever-C in-process landing (C8-sealed), M2 per-file materialization, an M1 per-file clone into an exclusive stage, and IR6 per-file skill stages.
  - Any activation built with copytree(symlinks=True), a directory clonefile or a git checkout is NOT by construction and takes the I2 walk. Today's "fresh activation rename" is therefore I2 until it is rebuilt per-file.
- **G2 (IR9 owner list): RESOLVED.**
  - The MD2 manifest landing proof joins the K8/IR9 allow-list as a proof kind, with the IR2 lifetime (dropped by `forget_invocation_memos` and `release_held`).
  - Its only producer is the fetch landing; its only consumers are package_plan/integrate.
  - It is published through the same guarded publish path (G3), not a separate dict write. The arch guard and its MutationCase cover it.
- **G3 (publish-race guard): RESOLVED as one rule.**
  - EVERY proof kind (link-free directories, the plan's link-free file set, the manifest landing proof) is published only if no forget ran since its observation began.
  - One shared generation, bumped by `forget_proofs`/`release_held` and by `forget_invocation_memos`, under one lock together with the clear; the #5382 9be8e258 shape.
  - Any forget invalidates ALL kinds: clear-all, never per-kind or prefix.
  - The proof that observed is the one checked: capture the generation before the first lstat/read/hash the proof rests on.
- **G4 (IR6 vs MS4 mode): RESOLVED, scoped.**
  - MS4 governs `apm_modules` materialization: mode from the git tree entry (0644/0755), set explicitly.
  - Deployed TARGET files keep today's deploy-mode rule exactly (whatever the current integrators apply per file, umask-derived included). It is set explicitly with fchmod on the staged file BEFORE the IR6 seal, and the seal checks mode too.
  - The differential compares modes per deployed file on L/M. On W, compare ACL inheritance under the #5388 rules (no explicit ACE added, none carried).
  - Never take a mode from the cache entry, the stage's umask default or a reflink source.
- **#5418 (lockfile portability): ACCEPTED.** The differential lists EXACTLY these three fields as known-variant: `anchored_local_path`, `declaring_parent: local:`, and the ledger `owners`/`active_owner` with absolute `local:` paths. It asserts byte equality on everything else. The #5418 fix removes the exemption and asserts it removed.
- **Warm path (MD(b) vs K2/IR1):** no conflict. On warm, C takes the I2 walk once per root; MD saves only the re-walk/re-classify.
- **MD3 hash-while-writing:** no conflict. Hash the buffer written to the target and re-check size on close; receipts stay byte-identical (differential).
- cost (W3d): 0 for each. G3 is one shared int + the lock already in #5382; G4 is one fchmod per staged file, which today's deploy already pays.

## 3.3 Cost summary

- Every invariant is O(1) per file or one per root/package.
- The only per-file additions over the fastest shape:
  - one open + fstat on read (already paid by the read);
  - one lstat on replace and per staged entry (seal);
  - one fchmod per materialized file;
  - one lock per proof publish.
- None changes the order of the measured ceilings (W 0.718 on main + #5374).
