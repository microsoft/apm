# APM v0.40.0 -- "5x of floor" install architecture (FINAL)

Owner: [PERF] Lead, Perf Architecture Research. Co-signers: W3d (security), PERF VP.
Status: FINAL, published 2026-11-25 ~08:10 CET (VP review edits closed; c2 struck 07:30 and every headline restated on output-verified heads; regent ruling 07:43 in section 6). Sources: SoL perf-sol-notes; Bets bets-notes.
Scope: `apm install` cold fresh, warm and locked journeys at tree-50 (50 deps, 684 deployed files).

## 0. Summary

- **Target** (reference runner class EPYC 9V74, cold t50, default-on, 0 errors): Linux <= 3.5 s, Windows <= 7.5 s.
  - Targets and all seconds in this doc are CPU p50 (user+sys, apm plus children), NOT stopwatch time.
  - Wall adds network/IO wait: on 9V74 at rtt=30, main+#5374 is 12.26 s wall vs 10.53 s CPU (+~1.7 s) [45534359514]. There is NO macOS target in v0.40; macOS is reported as a paired ratio only, since there is no 9V74-class calibration for macOS runners.
- **Main today (1e4a208e):** Linux 6.1 s, Windows 13.2 s. The physics floor is ~0.7 s Linux / ~1.2 s Windows, so the gap is 8.7x Linux / 11x Windows.
- **Where the gap is** (measured by paired deletion and a per-phase CPU probe on main):
  - integrate is ~45% Linux / ~50% Windows;
  - resolve + fetch is ~33%. The second copy + re-verify is only ~3-6% (faithful M2 / c2-copyfile 0.965 L; E1's budget ~6% CPU). The earlier 15-20% came from the invalid c2 bound.
- **Architecture: one manifest-driven deploy.**
  - The in-process fetch emits a per-package manifest (path, mode, content hash, primitive class).
  - Integrate consumes the manifest, with no re-walk, re-hash or re-classify. (Materialize-once, M2, was dropped: ~3-6% at most.)
  - Containment is proved by construction for APM-created trees.
- **Result** (stacked measured ceiling WITHOUT c2, one head per OS, unsafe upper bounds; OUTPUT-VERIFIED by render diff vs base: L ea0ec45e [45561435137], W 288927c8 [45561584579]. tree-10-mcp and tree-10-multi are byte-identical; monorepo-4 differs only by the #5418 tmp-path class; tree-50-policy cannot run on either side (harness POLICY_DOCUMENT defect); tree-50 is identical by elimination, because the per-fixture line was cut by log truncation; a path+mode+bytes re-run confirms it):
  - **Correction, stated plainly:** the earlier headline (W ~6.0 s "at target", L ~4.3 s) was INFLATED by an invalid bound. c2 (hardlink materialize) was fast because it skipped deploys: find_files_by_glob (base_integrator.py:1636-1639) skips files with st_nlink > 1, so every globbed primitive went undeployed. Found by E1 07:30 CET; c2 is struck, and so is every head that stacked it.
  - **W ~7.4 s on 9V74, AT the target line with no margin; 2 of 4 runner classes over (8573C, 7763).** W 288927c8 vs main+#5374 cc5aac78: 0.702, 18/18 [45558776176]. 10.53 s CPU base x 0.702 = 7.4 s. Per class: 9V45 ~5.6, 9V74 ~7.4, Xeon 8573C ~8.0, EPYC 7763 ~10.2-10.6.
    - Calibration: default-schannel world, main+#5374, cold t50, direct per-VM runs [45534359514]; base = CPU p50 (cpu_p50_s), not wall (wall p50 on 9V74 is 12.26 s). CPU base x paired CPU ratio keeps the units consistent.
    - Caveats: n = 1 VM per class (3 for 7763), 3 runs each; the paired ratio was measured on the bench's VM mix and is applied to one VM's base.
  - **L ~5.3 s vs the 3.5 s North Star.** L ea0ec45e vs main 1e4a208e: 0.875 [45558797634]; 6.1 s x 0.875.
  - M 0.717 [45558819800].
  - Materialize-once (Car 2, M2) is KILLED on the merits: a faithful single write reads W 0.979 / L 0.965 / M 0.891, and c2 with real copies (no hardlink) reads L 0.965 [45558963877]. So the hardlink skip was the whole c2 gain.
- **Native-core gate on Linux:** read as MET (walk+stat+hash+copy = 45% of L CPU [45520769932 / 45530101839]) but SUPERSEDED: that attribution counted fs-named Python frames, not syscall CPU. Zero-cost oracles free ~5%, and Lane N was KILLED (section 8).
- **Linux bar (REGENT RULING):** L <= 3.5 s stays the North Star; v0.40 ships the Python manifest architecture at the measured, output-verified stack, ~5.3 s (restated from ~4.3 s after the c2 strike; regent 07:43, section 6). A native fs-op core was a 48 h research lane (Lane N), KILLED WED 05:12 (section 8). No measured Python path reaches 3.5 s (section 7-8).

## 1. Physics floor per phase on main (PERF-SOL)

Scope: `apm install`, cold (no lockfile, empty cache), tree-50 fixture (50 packages, 89 edges, 11 primitives
per package, MCP server in every 8th package), anonymous, main 1e4a208e. All numbers are CPU (user+sys,
apm plus children) measured on CI. Nothing was measured locally.

### How each column was measured
- **Share**: a research-only per-phase CPU probe on main (`perf-research/arch-main-phasecpu` 7a7e77cf,
  `APM_RESEARCH_PHASECPU=1`). It records process CPU across all threads around each pipeline phase,
  plus boot CPU at pipeline entry. Median of 3, run 45526028119.
- **Deletion**: paired 6-pair bench with the phase removed (`perf-research/arch-main-del-*`), anon,
  W = default schannel world. Runs: integrate 45520602660 / 45520678153; lockfile+ledger+journal
  45520632797 / 45520705563; policy 45520658679 / 45520729238; reachability 45526077102 / 45526093339.
- **Seconds at 9V74**: share x the main t50 reference on the EPYC 9V74 runner class (L 6.1 s, W 13.2 s;
  calibration F16, run 45457579415). Absolute CPU moves up to 2x between runner classes, so seconds
  are only comparable within one class.
- **Floor**: counts on main (run 45520678153: 101 requests, 111 round trips, 5 connects, 18 git spawns,
  949 modules; 667 deployed files, about 700 package files) x unit costs measured on hosted runners
  (unit-costs.md, run 45359600677).

### Unit costs used (hosted runners, process CPU)
| unit | L | W |
|---|---|---|
| interpreter start (`python -c pass`) | 9 ms | 19 ms |
| `apm --version` (CLI boot proxy) | 94-116 ms | 125-172 ms |
| file create+write, O_EXCL | 16 us | 67 us |
| hardlink from store | 20 us | 56 us |
| read 1 KB | 8 us | 25 us |
| realpath (depth ~8) | 34 us | 53 us |
| lstat | 2.5 us | 3.1 us |
| sha256 1 KB / per MB | 0.8 us / 0.5 ms | ~0 / 1 ms |
| scandir of a 28-entry dir | 15 us | 22 us |
| keep-alive HTTPS GET (github.com) | 1.1 ms | <15 ms (timer floor) |
| new TLS connection + GET | 4.7 ms | 5.9 ms |
| trivial git spawn (parent + child) | 1.1 ms | 21 ms |

### The table (cold t50, main)

| phase | share L / W (probe) | ~s at 9V74 L / W | deletion ratio L / W | floor L / W | gap L / W | what the floor is |
|---|---|---|---|---|---|---|
| boot (interpreter + imports + CLI) | 9.8% / 4.8% (0.46 / 0.75 s) | 0.6 / 0.6 | n/a (F15) | 0.15 / 0.25 | 0.45 / 0.35 | interpreter + the modules install needs; `--version` is 0.1 / 0.15 s |
| resolve + fetch (in-process; download phase ~0) | 33.8% / 33.5% (1.58 / 5.28 s) | 2.1 / 4.4 | reachability only: 0.994 / 0.992 | 0.20 / 0.35 | 1.9 / 4.0 | 101 pooled requests (~0.11 L), 5 TLS connects, pack inflate+hash 0.6 MB (<0.01), ~700 cache files + ~700 apm_modules files written (0.02 L / 0.09 W; hardlinks would be the same), 50 apm.yml parses (~0.05) |
| integrate (all families + shared per-file pipeline) | 41.5% / 50.9% (1.94 / 8.03 s) | 2.5 / 6.7 | 0.551 / 0.499 | 0.15 / 0.25 | 2.4 / 6.5 | 667 deployed files: read + frontmatter parse + write + hash (~0.15 ms L / 0.25 ms W per file), one containment proof per target dir, skill dirs linked |
| . skills family | (deletion) | 0.8 / 1.9 | 0.871 / 0.854 | | | 2 skills/pkg (SKILL.md + reference) |
| . instructions / prompts+commands / agents | (deletion) | 0.3+0.3+0.2 / 0.95+0.7+0.5 | 0.947, 0.956, 0.973 / 0.927, 0.944, 0.962 | | | 4 / 3 / 2 per pkg |
| . shared per-file pipeline (integrate minus families) | (deletion) | 1.2 / 2.4 | ~19.6% / ~18.8% | | | containment, receipts, ownership, discovery re-walks |
| lockfile build + write | 3.0% / 2.5% (0.14 / 0.39 s) | 0.2 / 0.3 | n/a alone | 0.05 / 0.08 | 0.15 / 0.25 | one YAML emit (50 deps + 667 hashes, C emitter) + one atomic write |
| lock + ledger + journal (deletion) | CONFOUNDED | n/a | 0.770 / 0.789, CONFOUNDED | | | Deleting the lockfile build/write makes downstream phases skip their work (MCP/LSP update_lockfile, post_deps_local, claude_plugins). lockfile.py itself is ~3% L inclusive [45521813477, E2], and the finer deletions (commit gate, claim binding, disk write) are neutral. The REAL costs in this area are the ledger _trusted_hash lstat chain (~3,300 lstat at t50, W ~0.91 s), counted under integrate/containment, and the journal (L ~7%, inside integrate). No lock-phase lever. |
| policy / target check / audit / content scan | ~0 phase CPU (scan runs inside integrate) | 0.12 / 0.5 | 0.980 / 0.961 | 0.03 / 0.05 | 0.1 / 0.45 | regex scan of ~0.7 MB of text |
| MCP/LSP services reconcile | probe 6.8% / 3.4% = other threads overlapping that window | ~0 | 1.005 / 1.005 (main, 45527709409 / 45527692033) | ~0.01 | ~0 | DISCREPANCY CLOSED: the paired deletion on main is neutral. The probe's process CPU in that window comes from concurrent threads, not the phase. |
| glue (pipeline setup, session construction, exit) | 4-5% (0.2 / 0.7 s) | 0.3 / 0.65 | n/a | ~0.02 | 0.3 / 0.6 | |
| **total** | 4.68 / 15.8 s on the probe runner | **6.1 / 13.2** | | **~0.7 / ~1.2** | **~5.4 / ~12** | measured/floor = **~8.7x L, ~11x W** |

### Materialization re-measured on main (resolve+fetch row): the copy into apm_modules
Today, per file: the in-process fetch writes the cache checkout; `robust_copy_entry` copies it into
apm_modules (github_downloader.py:1922-1926); `require_verified_copy` re-reads and re-verifies the copy (:1927).
Paired 6 pairs on main 1e4a208e, anon, t50:
| stub (research only) | cold W | cold L | cold M | warm W | warm L | runs (cold W/L/M; warm W/L) |
|---|---|---|---|---|---|---|
| (c1) hardlink instead of copy, verify kept (UNSAFE, upper bound only; W3d M3 rejected) | 0.832 | 0.832 | 0.850 | 0.838 | 0.827 | 45527826943 / 45527848039 / 45527865311; 45527994568 / 45528022070 |
| (c2) hardlink, no re-verify = one write per file (M2 / fetch-to-final-tree upper bound) | 0.802 | 0.817 | 0.856 | 0.833 | 0.794 | 45527883690 / 45527902064 / 45527922103; 45527956593 / 45527977857 |
- The second copy plus its re-verify is **15-20% of cold on every OS** (~1.0-1.1 s L, ~2.2-2.6 s W at 9V74)
  and **16-21% of warm**. The re-verify alone is ~1.5-3% (c1 vs c2).
- Per file that is ~1.4 ms L / ~3.5 ms W against a raw write of 16 / 67 us: the cost is Python per-file
  copy machinery (copytree, copy2, copystat, robust wrappers), not bytes. That is why macOS (APFS, where
  a clone could be cheap) shows the same ~15%.
- Shapes (W3d w3d:ruling:materialize-once):
  - **M2, cache = objects only, fetch writes the final tree** (OK under MS1-MS7). Cost line: 1 write per
    file (was 2), bytes bound by re-hash-as-written (MS1), O_EXCL/NOFOLLOW per file (MS3), mode from the
    tree entry (MS4). Ceiling ~ the c2 row: ~18-20% cold. On warm it needs inflate + write from the
    object store; c2-warm (0.79-0.83) bounds it.
  - **M1, reflink/COW** (OK under MS1-MS7). Cost line: per file 1 create + 1 clone + 1 fchmod, 0 data
    bytes. CI runners can't show it (ext4 / NTFS have no clone; macOS already 0.85 with c1). Because the
    cost is per-file Python, not bytes, M1 buys little over a lean copy loop. M2 is the lane.
  - M3 hardlinks: rejected for any user- or project-nameable tree (shared inode).
- This makes "materialize once" a v0.40 lane BESIDE integrate: ~1.0 s L / ~2.4 s W of the 2.1 / 4.4 s
  resolve+fetch block.

### Reading
1. **Integrate is half the gap on both OSes** (2.4 s L, 6.5 s W at 9V74). Per deployed file, main spends
   ~2.9 ms L / ~10 ms W against a floor of ~0.15 / 0.25 ms: ~20x L, ~40x W. The cost is not bytes.
   It is per-file Python orchestration (containment, receipts, ownership checks, re-walks) on top of
   each family's own work. Skills cost 4-5x a file primitive per item.
2. **Resolve + fetch is the second block** (2.1 s L, 4.4 s W), about a third of cold, and has no deletion
   lever yet. Within it (profile 45520749499 / 45520769932):
   - download_package (in-process fetch + cache checkout + copy into apm_modules) is 3.7 s W / 1.9 s L
     of profile CPU, of which _persistent_cache_checkout is 1.6 W / 0.7 L and fetch_worktree 1.2 W;
   - manifest loading and graph work is ~1.2 W / 0.4 L;
   - reachability is under 1% (deletion 0.992-0.994).
   Network bytes and request counts are already at floor (101 requests, 0.6 MB). Measured (section
   above): the second copy + re-verify alone is 15-20% of cold on L/W/M, so "materialize once" (M2) is
   a v0.40 lane.
3. **Lock + ledger + journal: no lock-phase lever.** The lockfile build itself is 0.14 / 0.39 s (~3%). The
   21-23% deletion is a confound: without a lockfile, downstream phases skip their work. The real
   costs sit inside integrate: the ledger's _trusted_hash lstat chain (~3,300 lstat, W ~0.9 s; a
   containment-family cost, see section 2) and the deploy journal (L ~7%). The records split (F20) found
   no byte-identical batching that reaches 5%. This needs a records-model redesign (per-package records),
   the v0.40 headline.
4. **Boot is no longer negligible on L** (~10% of cold). Imports are flat at ~0.4-0.5 s while cold shrank,
   but lazy imports measured nothing (F15). The floor needs fewer modules on the install path.
5. **Policy/audit/scan and reachability are small** (<= 4%). Not design-doc levers.
6. **MCP services**: closed. The paired deletion on main is 1.005 / 1.005. The probe's 0.3-0.5 s was CPU
   from concurrent threads in that window.

### Caveats
- Deletion confounds: a deleted phase can make a downstream phase skip its work. Integrate deletion is
  checked against the direct probe (41.5% / 50.9% probe vs 45% / 50% deletion: consistent). The
  reachability deletion only skips a verification call, so nothing downstream depends on it. The
  lock+ledger+journal deletion IS confounded (above).
- Per-phase shares come from one probe run (3 samples, OpenSSL world on W via the older trace harness).
  Deletion ratios come from paired 6-pair runs (W default schannel). They agree within ~5 points where
  they overlap (integrate 41.5% vs 45%, W 50.9% vs 50%).
- Windows getrusage children are unavailable in the probe. Job-object totals say W children are ~0.3 s
  on cold (18 git spawns).
- Floors are first-principles estimates from measured unit costs; file-count and parse costs are estimated.
  The floor total (~0.7 s L / ~1.2 s W) matches the morning SoL model (0.7 / 1.5).

### Stacked ceiling: the doc's headline number (measured on one head, not a paper sum)
Head = Bets' integrate ceiling (perf-research/arch-integrate-ceiling-skilltree ea0ec45e: main + A+B +
parse-once + skill tree as one op; UNSAFE upper bound) + my M2 bound c2 (hardlink, no re-verify; UNSAFE).
L/M: perf-research/arch-stack-ceiling-L d3881480 vs main 1e4a208e. W: perf-research/arch-stack-ceiling-W
2a69add2 vs its base cc5aac78 (main + #5374), default schannel world. Cold t50, 6 pairs, anon, 0 errors.
| OS | paired ratio | base -> new (bench runner) | ~s at 9V74 | target | run |
|---|---|---|---|---|---|
| L | 0.701 | 7.18 -> 5.03 s | 6.1 x 0.701 = **4.3 s** | 3.5 s (miss by ~0.8 s) | 45530064444 |
| W | **0.568** (vs main+#5374; head W2 6d67f5aa) | 15.09 -> 8.44 s | 10.53 (schannel 9V74 base, main+#5374) x 0.568 = **~6.0 s** | 7.5 s (MET by ~1.5 s; 3 of 4 runner classes) | 45532025700 / calib 45534359514 |
| M | 0.663 | 8.18 -> 5.37 s | n/a (no 9V74 class) | - | 45530082924 |
W correction: the first W head (2a69add2, 0.719) lacked the lexical-resolve ceiling on Windows, because
realpath.py redefines resolve_path inside `if sys.platform == "win32":` and the L ceiling's stub sat in
the generic function. W2 6d67f5aa = Bets' W ceiling 8efedece (lexical override at end of file) + c2 +
parse-once + skill tree. W seconds use the default-schannel calibration [45534359514]: main+#5374 cold t50 is
10.53 s on 9V74 (9V45 8.0, Xeon 8573C 11.3, EPYC 7763 14.6-15.1). Stacked W is ~6.0 s on 9V74 and
8.3-8.6 s on 7763, the only class over 7.5 s.
Overlap vs paper (L): c2 alone 0.817, ceiling alone 0.899 (Bets 45528038721). Paper additive = 0.716,
multiplicative = 0.734; measured 0.701. They stack slightly better than the paper (removing the copy also
removes integrate-side walks of the copied tree). Per-shard ratios within +-1.5%.

### Native-core gate (charter: "native only if the profile shows >= 25% of CPU in walk/stat/hash/copy")
cpu_attr leaf kinds, L process CPU, one profiled sample each:
| head | walk_scan | stat | hash | file_io (copy/read/write) | gate sum | + realpath | run |
|---|---|---|---|---|---|---|---|
| main 1e4a208e | 10.7% | 14.6% | 0.2% | 19.9% | **45.5%** | 54.2% | 45520769932 |
| stacked ceiling d3881480 | 18.1% | 11.6% | 0.2% | 15.3% | **45.2%** | 65.1% | 45530101839 |
**Gate MET on main and at the stacked head (45%, >= 25%).** Hash is negligible (0.2%): bytes are not the
cost. The native-core candidate is the walk/stat/realpath/file-op layer (syscall-heavy Python), not hashing.
Even at the stacked ceiling, 65% of L CPU is filesystem-syscall Python, plus 13% imports.

## 2. Integrate redesign (PERF-Bets)

Owner: PERF-BETS. Status: W3d CO-SIGNED section 2.1 with IR1-IR9 (row `w3d:ruling:integrate-2.1`). All numbers are cold tree-50, paired CPU incl. children, against the base named in each row.

### 2.0 Goal and bar

- Integrate is 50% of W cold (6.5 s) and 45% of L cold (2.7 s) on main 1e4a208e (SoL split; W runs default Git for Windows / schannel).
- Kill bar: integrate CPU <= 50% of today, which is -3.3 s on W and -1.35 s on L.
- No single family is big enough on its own:
  - skills 14.6% of W;
  - the shared per-file pipeline 18.8% of W / 19.6% of L;
  - containment is the W cost driver (6.2 s of 16.8 s in the W profile).

### 2.1 Security invariant (W3d terms) -- stated before any shape

The redesign must be NO WEAKER than today's per-path containment. In W3d's `containment-once` terms (K1-K9):

- **I1 (by construction).** A destination or source tree that APM created in THIS invocation, with exclusive `mkdir` and no-follow `CREATE_NEW` writes, holds no link that APM did not write. That is K2. Its paths need no per-path resolve.
- **I2 (one resolve per root).** Every other tree (deploy roots such as `.github/` and `.claude/`, the project root, and trees from earlier runs) is proven link-free ONCE per command. The proof is a full resolve of the root plus an lstat walk of the components the plan will use. It lives in `invocation_memo` (K1) and is dropped:
  - by `forget_invocation_memos` after any user code (lifecycle scripts, hooks);
  - by `release_held` -> clear-all forget before any APM rename, delete or link creation (R2/R4, as in #5382 c8eb343f).
- **I3 (leaf refusal).** Under a proven root, each file write is `CREATE_NEW` / `O_NOFOLLOW` for a new leaf. For an existing leaf: lstat-refuse a link or reparse point, then atomic replace (K3/K5, never in place). Each new directory component is created exclusively and proven before first use (K4). Per-file cost is O(1): one open, plus one lstat when replacing.
- **I4 (lexical relpaths only under a proof).** Locators, receipts and relative paths take the lexical form only when the proof saw no in-root link, so lockfile bytes are unchanged (K6). Otherwise they use the full resolve, as today.
- **I5.** Exact-text keys (K7). One owner, `utils/realpath.py` plus the new `install/package_plan.py`, with an arch guard (K8). A differential covers mid-install user code and a warm planted link (K9).
- **Out of model** (as ruled): a concurrent same-user process racing the command.

#### Map: today's per-path checks -> replacement (counts from the L profile of main, run 45523400455)

| today's check | calls / tree-50 | where | replacement | invariant |
|---|---|---|---|---|
| `ensure_path_within_resolved` + `has_symlink_component` per source file | 2067 | `deployable_source_plan._is_safe_source_path` (`safe`, `copy_ignore`) | One no-follow walk per package at plan time records the link-free file set. A tree created this invocation (in-process fetch / checkout landing) is link-free by construction (I1). An earlier-run tree is walked once (I2). Per file: a set lookup. | K2, I1/I2 |
| `ensure_path_within` + `has_symlink_component` (security gate scan) | 667 | `security/gate._explicit_scan_candidate` | Reads the same plan file set; the scan still reads every byte it scans. | K2, K3 |
| `ensure_path_within` (destination) | 650 | `copy_directory_files`, `integrate_{instructions,prompts,agents}_for_target` | Deploy root proven once (I2); per file a lexical join under the root, then I3 (CREATE_NEW / lstat-refuse + atomic replace). | K3/K4/K5 |
| `validate_deploy_path` + `has_symlink_component` | 200 | `skill_preflight.find_unmanaged_conflicts` | The same root proof plus a leaf lstat (a conflict check is a read). | I2, I3 |
| `resolve_for_containment` (ledger locators) | 3618 + 2734 | `deployment_ledger.locator_for_path`, `deployed_paths._resolved_candidate` | Locator = the plan's lexical rel under a proven root (I4); full resolve when unproven. | K6 |
| `resolve_path` via `portable_relpath(s)` | 5652 | receipts, lockfile `deployed_files` | Same as above (I4). | K6 |
| `resolve_path` in link resolver / skill roots / globbing | ~3000 | `init_link_resolver`, `_inactive_skill_roots`, `find_files_by_glob` | Computed once per package in the plan (Shape A). | K1 |

| `_trusted_hash` lstat chain (ledger) | ~3,300 lstat; W 0.91 s | `core/deployment_ledger` | Root proof plus leaf lstat (I2/I3). E2 is measuring the overlap; it is counted once, inside the containment family, and not double-counted in the W ceiling. | K1, I2 |

#### W3d co-sign: row `w3d:ruling:integrate-2.1`, REQUIRED IR1-IR9

Without IR1/2/4/5 the design is weaker than today.

- **IR1.** I1 covers only trees APM itself wrote with no-follow exclusive creates (the in-process / lever-C landing). A git checkout materializes symlinks, so it takes the I2 walk.
- **IR2.** The plan's link-free set has the invocation-memo lifetime. It is dropped on user code and on `release_held` (clear-all, generation-guarded as in #5382 9be8e258).
- **IR3.** Every source READ stays no-follow, plus an `fstat` S_ISREG check.
- **IR4.** On nt, refuse ANY reparse point (junctions fail S_ISLNK).
- **IR5.** One component validator runs before any lexical join: `..`, ADS `:`, trailing dot/space, device names, 8.3 `~N`.
- **IR6.** Shape B builds the stage per regular file from the plan set (never a clonefile/copytree of the directory), C8-seals it, and moves an existing directory aside through the journal.
- **IR7.** Locators are computed against the single resolved root. K9 includes case-variant and 8.3 roots.
- **IR8.** The same per-file outcome and diagnostic when a link is found.
- **IR9.** The K8 guard plus a MutationCase. K9 adds:
  - a planted link in an old `apm_modules` tree;
  - a `.claude` junction;
  - a script planting a link between plan and deploy;
  - a git checkout containing a symlink.
- cost: O(1) per file or per root; the ceiling is unchanged.

#### Receipts and ownership (unchanged contracts)

- **Receipts.** Every deployed file is still recorded with its content hash in the lockfile and deployment receipts. The hash is computed from the bytes written (for a staged skill tree, in one pass over the staged tree). The differential (2.5) proves the lockfile and receipts are byte-identical.
- **Ownership.** A destination is written only when it is absent or APM-owned per the deployment ledger. That is checked against the plan before any write (today's `check_before_deploy` / `find_unmanaged_conflicts`, same semantics). A skill tree swap requires the destination directory to be absent or ledger-owned in full.
- **Deploy journal.** Unchanged per file for non-skill primitives. A staged skill tree is one `dir` unit (`deploy_snapshot` already supports `dir` units with `_tree_hashes`). Replay semantics are unchanged.

### 2.2 Shapes

#### A. One `PackagePlan` per package (shared pipeline)

- Computed once at materialization, consumed by every integrator:
  - package type, primitive inventory (one walk) and link-free file set;
  - parsed `apm.yml`;
  - link-resolver contexts;
  - package content hash;
  - PARSED primitives (one parse per file, see 2.4).
- Today these are re-derived 2-5x:
  - `detect_package_type` and `init_link_resolver`: 250 calls each (5 integrators x 50 packages);
  - `validate_apm_package` and `compute_package_hash`: 100 each;
  - 3 inventory walks per package;
  - `loads_frontmatter`: 950 calls, ~19 per package.
- **Cost line, per package:** 1 walk (was 3), 1 detect (was 5), 1 hash (was 2), 1 parse per primitive file (was ~3-4). Spawns 0.

#### B. Bulk deploy: skill bundles as one staged tree

- Reflink copytree into a stage, one tree hash, one atomic rename, one `dir` journal unit. Containment by construction (I1).
- **Cost line, per skill:** 1 copytree + 1 rename + 1 tree hash (was N writes + 2N journal events + N containment checks).

#### C. Containment by construction for destinations

- I1-I4 above, applied to every integrator's writes.
- This is the W lever. It replaces today's per-path resolve; it does not remove the check.

### 2.3 Measured ceilings (research upper bounds, deliberately unsafe; ceilings only)

Base main 1e4a208e:

| head | W | L |
|---|---|---|
| A memo (576929fa) | 0.970, -0.9 s [45524106186] | 0.976, -0.15 s [45524085463] |
| A + journal off (532d7a1f) | 0.969 [45525632628] | 0.936 [45525615896] |
| A + lexical resolve / no link scan (893b7b89) | 0.752 [45525667777] | 0.947 [45525650620] |
| A + both (ea46241b) | 0.688, -5.8 s [45524926467] | 0.901, -0.54 s [45524908282] |
| A + both + parse-once (4c98c85e) | - | 0.914, 6.96 -> 6.14 s, -0.82 s [45526633274]; same-window rerun of A + both: 0.920, -0.45 s [45526647663] |
| A + both + parse-once + skill tree as one op (ea0ec45e; copytree, which IR6 forbids in product, so this is an upper bound) | - | 0.899, 4.68 -> 4.22 s [45528038721]; same-window parse-once 0.903 [45528060785]: the skill tree adds ~0 |

Base main + #5374 (cc5aac78; the W claim requires this row, per VP):

| head | W |
|---|---|
| A memo (1b0a0ead) | 0.975 [45526483374] |
| A + lexical/no link scan (f91d724d) | 0.793 [45526498364] |
| A + both (8efedece) | 0.718, 15.36 -> 10.91 s, -4.45 s [45526516043] |

Reading:
- W on main + #5374 still clears the bar: -4.45 s vs -3.3 s. #5374 does not remove the containment share; containment by construction is the W lever.
- L misses the bar even with parse-once and skill trees: the L ceiling is stuck near 0.90 of cold (bar ~0.775). A profile of the ceiling head is running to find the residual. Previously: best -0.82 s vs -1.35 s (about 30% of L integrate). L <= 50% needs B (skill trees as one op) plus cheaper copies/writes, measured next.
- On W, the containment family carries the win (0.970 -> 0.752). The journal is ~0 on W.
- On L, the journal (-4%) and resolve (-3%) are both small. L's remainder is content transforms: frontmatter re-parse, `write_text_lf` and copies, about 0.8 s real.

#### Verdict

**A+B+C clears integrate <= 50% on W (main + #5374: 0.718, -4.45 s vs -3.3 s), not on L (L ceiling ~0.90 of cold vs a ~0.775 bar).**

#### L remaining cost after every hack

Ceiling head ea0ec45e, cProfile, about 2x real. 684 deployed files. `integrate.run` is 4.19 s profiled, about 6.1 ms per file.

| owner | profiled s | ms/file | what it is |
|---|---|---|---|
| `primitives/discovery._find_primitive_inventory_files` | 0.79 | 1.15 | per-type (3x) glob classification of the inventory, plus `is_file`/`is_symlink` per candidate; Shape A-faithful classifies once |
| `install/template.preflight_skill_materializations` (`find_unmanaged_conflicts` 0.27) | 0.56 | 0.82 | skill ownership/conflict preflight |
| `install/sources.acquire` | 0.51 | 0.75 | manifest snapshot 0.13, carried hash 0.11, license 0.08, targets 0.06, `apm.yml` 0.06 |
| skill copy residual | 0.50 | 0.73 | stale-copy report 0.16, copytree 0.10, link render 0.07, ledger record 0.05 |
| `parse_primitive_file` | 0.22 | 0.32 | one parse per file (already once) |
| prompts / instruction prep / gate scan / agents | 0.17 / 0.13 / 0.13 / 0.09 | 0.25 / 0.19 / 0.19 / 0.13 | per-target render |

- No owner exceeds ~20% of what remains.
- The next L lever is classification once (about -0.35 s real), then the skill preflight. Even with both, L stays near ~0.85.
- On L, integrate <= 50% needs a different design (no per-file Python work at all, e.g. a manifest-driven deploy from the in-process fetch manifest) or a different L bar.

### 2.4 Parse-once: one parsed model per primitive, N renders

- **Model.** `PackagePlan.primitives[path] = ParsedPrimitive(metadata, body, raw_bytes_hash)`, parsed once with the bounded loader (`yaml_io.loads_frontmatter`, same hostile-input limits).
- Each integrator x target RENDERS from the model, never re-reading or re-parsing the file.
- Renders must not mutate the shared model: integrators receive a read-only view; any per-target change copies.
- **Invariant.** The model is keyed by exact path text plus the content hash of the bytes read at plan time. The plan is dropped with the invocation memos (user code), and on any APM write to the source tree (release_held forget).

### 2.5 Acceptance differential (byte-identical render)

- **Harness.** `perf-research/arch-render-diff`, `.github/probe/render_diff.py`. In each offline fixture world, cold-install with the BASE CLI and with the CANDIDATE CLI. Then snapshot every file under the project, excluding `apm_modules/` and `.git/`:
  - deployed primitives for every target;
  - MCP configs;
  - hooks;
  - the lockfile minus timestamp lines;
  - deployment receipts.
- It compares sha256 per path, and fails on any added, missing or differing path.
- **Fixtures:**
  - `tree-50`: instructions, prompts, agents, skills, sub-skills, diamonds;
  - `monorepo-4`: several `apm.yml` roots;
  - `tree-10-mcp`: a stdio MCP server per package;
  - `tree-10-multi`: GitHub, GHE Cloud, GitLab and ADO hosts.
- Extend with `-policy` variants for discovery/enforcement and a Windows run for CRLF/`core.autocrlf` paths. Note: `-policy` fixtures need the harness POLICY_DOCUMENT fix (mcp `trust_transitive: True`); without it every MCP-bearing tree is blocked on the base CLI (tests/fixtures/perf/offline.py:49).
- **Cells (the product PR's tests).**
  1. Differential over the fixture list above on L, W and M.
  2. A primitive whose frontmatter one integrator would mutate is rendered identically for a second target (copy-on-write view).
  3. A source file changed by user code mid-install (lifecycle script) is re-parsed (memo dropped).
  4. Hostile frontmatter (alias bomb, `<<` merge) still refuses through the bounded loader, parsed once.
- **Product finding #5418.** A monorepo lockfile embeds absolute checkout paths for sibling local packages (`anchored_local_path`, `declaring_parent: local:/...`, ledger `owners`), from `deps/local_identity.py:portable_anchor_identity`'s absolute fallback. Its delta stays visible in the differential until the fix lands.
- **Results:**
  - A/A (main vs main, run 45526809921): `.apm-local/installation` (a per-install id) differs in every fixture, and the 4 monorepo app lockfiles differ (side-dependent content). These are harness noise; the harness must exclude them.
  - Parse-once upper bound vs main (run 45526789209): EXACTLY the same diff set as A/A and nothing else. Every deployed primitive, MCP config, hook and receipt is byte-identical, as are the tree-50/mcp/multi lockfiles.
  - Harness TODO: exclude `.apm-local/installation` and explain the monorepo lockfile delta, so IDENTICAL means zero.

### 2.6 Build order and gates

1. A (plan plus parse-once). Gate: differential IDENTICAL, L+W+M cold t50 benches.
2. C (destination containment by construction). Gate: W3d review of the 2.1 map, the K9 differential, and the W bench on main + #5374.
3. B (skill trees as one op). Gate: differential, receipts identical, and a journal replay cell.

## 3. Materialize-once (M2)
- Today each file is fetched into the cache, copied into apm_modules, and the copy is re-verified (github_downloader.py:1922-1927).
- Deletion bound (c2): cold W 0.802 / L 0.817 / M 0.856; warm W 0.833 / L 0.794.
- The cost is Python per-file copy machinery (~1.4 ms Linux / 3.5 ms Windows per file).
- W3d ruled M1 and M2 OK (MS1-MS7) and M3 hardlinks rejected.

## 4. Manifest-driven deploy (the architecture)
- The in-process fetch emits a per-package manifest (path, mode, content hash, primitive class).
- M2 materialization and integrate consume it, with no re-walk, re-hash or re-classify.
- Trust statement (W3d co-signed, row w3d:ruling:manifest-deploy, VERBATIM):

> "The manifest is NOT a trust root: every content hash in it was computed by the fetch over the exact bytes it wrote AND equals the blob id the pinned commit's tree names (MS1); it is a one-use proof bound to this command's landing and dropped by any user code or APM mutation of that tree (IR2); deployed-file receipts are still hashed over the bytes written to each target; the primitive class comes from integrate's single classifier applied to the manifest's path set (IR5 validator, K8); and any package with an entry that cannot be proven this way (git-fallback, carried/warm tree, reparse point or link, mode mismatch, unexpected entry) takes today's per-file walk, hash and classify (MS5)."

- Warm installs: a cached manifest file is never trusted.
  - It is re-derived from the pinned commit's tree objects (re-hashed, MS6), and the apm_modules bytes are bound by verify_locked_package.
  - So warm saves only the re-walk and re-classify.
  - An optional accelerator cache is keyed (commit, schema version, build id) and compared exactly with the object-derived set.
- Cost: ~0 per MD item; warm adds only tree-object inflation.

## 5. Security invariants, consolidated (PERF-Bets; W3d co-signed)

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

### 5.1 Invariant table

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

### 5.2 Conflicts and gaps between rulings: RESOLVED

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

### 5.3 Cost summary

- Every invariant is O(1) per file or one per root/package.
- The only per-file additions over the fastest shape:
  - one open + fstat on read (already paid by the read);
  - one lstat on replace and per staged entry (seal);
  - one fchmod per materialized file;
  - one lock per proof publish.
- None changes the order of the measured ceilings (integrate ceiling W 0.718 on main + #5374; stacked W 0.568).

## 6. Targets -- REGENT RULING (restated 07:43 CET 25 Nov, via PERF VP; supersedes the 03:25 ruling)
Output audit PASSED on the no-c2 heads (L ea0ec45e [45561435137], W 288927c8 [45561584579]; see section 0), so the numbers below stand as measured.
- v0.40 L milestone = "the measured, output-verified stack, ~5.3 s (restated from ~4.3 s after the c2 strike)". Basis: L ea0ec45e vs main 0.875 [45558797634].
- L <= 3.5 s North Star unchanged, no measured path (v0.41+ research).
- W <= 7.5 s target unchanged: "at the line, no margin, 2 of 4 runner classes over". Basis: W 288927c8 vs main+#5374 0.702 [45558776176]; ~7.4 s on 9V74 (schannel CPU calibration [45534359514]).
- Strike note: the 03:25 ruling's ~4.3 s L basis (stacked 0.701) and the W 0.568 (~6.0 s) are WITHDRAWN; they included c2, a hardlink bound that skipped deploying every globbed primitive (nlink guard, base_integrator.py:1636-1639).
- The native fs-op core research lane (Lane N) was KILLED WED 05:12 (section 8); the 45% native-core gate was superseded (it counted fs-named Python frames, not syscall CPU).

## 7. Build plan (PERF-Bets; owners per PERF VP)

Rules for every car:
- Default-ON, no flag. A kill switch (env) is allowed only as an escape hatch that keys the verdict record.
- Lands alone on main. Benched at its FINAL head vs its merge-base: cold+warm t50 L/W/M, 6 pairs, paired CPU incl. children.
- Passes the render/materialization differential with zero diffs. The known-variant fields are exactly #5418's until #5418 lands; nothing else.
- Has its W3d row(s) and QL H8 before merge.
- Expected ratios come from MEASURED ceilings (section 2.3 / SoL section 1). A faithful car is expected to keep 50-80% of its ceiling. Kill bars are on the faithful car.

### Car 1: A, plan once per package (classify, inventory, parse once)

- **Scope.**
  - `install/package_plan.py` (new owner): one inventory walk, ONE classification pass, one `apm.yml` parse, type detection, link-resolver contexts, package hash, and parsed primitives (one parse per file, copy-on-write view per render).
  - Integrators consume the plan; an arch guard stops them calling discovery/detection directly.
- **Measured ceiling** (memo-only upper bound, without classify-once):
  - main: W 0.970 [45524106186], L 0.976 [45524085463];
  - main + #5374: W 0.975 [45526483374].
  - Classify-once is the largest L residual (1.15 ms/file profiled, about -0.35 s real at t50).
- **Expected faithful:** W ~0.97, L ~0.95-0.96 cold.
- **Kill bar:** <= 0.98 cold on L AND W at t50; else fold into Car 5.
- **W3d:** IR2 (plan lifetime), IR5, IR8, MD4/K8/IR9 (single classifier + guard), T1/T2 publish guard. New row: plan-once implementation.
- **Differential:** render diff (tree-50, monorepo-4, tree-10-mcp, tree-10-multi, plus `-policy` variants and a Windows run). Plus cells:
  - copy-on-write render;
  - source changed by a lifecycle script is re-parsed;
  - hostile frontmatter still refused.

### Car 2: M2, materialize once (fetch into the final tree; cache = objects)

- **Scope.**
  - The in-process fetch lands per regular file into an exclusive stage under `apm_modules` (MS3), C8-seals it and lands it atomically. The cache keeps objects only, re-hashed on read (MS6).
  - Activation stops copying a cache checkout. M1 (reflink/COW) is optional on warm.
  - Unprovable content goes to git into a stage, then the same land + verify (MS5).
- **Measured bound (SoL c2, main):** cold W 0.802 / L 0.817 / M 0.856; warm W 0.833 / L 0.794. That is the second copy + re-verify, 15-20% of cold on every OS.
- **Expected faithful:** ~0.85-0.88 cold on all OSes.
- **Kill bar:** <= 0.90 cold on at least 2 OSes.
- **W3d:** MS1-MS7. New implementation row. IR1 note: an M2 landing IS by-construction for C (resolves gap 3.2-1).
- **Differential:** MS7. Lockfile, receipts, deploy store, and `apm_modules` bytes AND modes identical on L/W/M. Plus cells:
  - tampered cache object refused;
  - user edit in `apm_modules` caught by `verify_locked_package`;
  - an eviction in project A leaves project B untouched.

### Car 3: C, containment by construction (destinations and sources)

- **Scope.**
  - One proof per deploy root per command (I2); trees from Car 2 / in-process landings are by-construction (IR1).
  - O(1) leaf handling: CREATE_NEW / lstat-refuse + atomic replace; any reparse point refused on nt.
  - Lexical relpaths/locators under a proof only (K6/IR7). One write owner (K8).
  - The ledger's `_trusted_hash` lstat chain moves under the root proof (counted once).
- **Measured ceiling (lexical resolve + no link scan, on top of A memo):**
  - main + #5374: W 0.975 -> 0.793 [45526498364];
  - main: W 0.752 [45525667777], L 0.947 [45525650620].
- **Expected faithful:** W ~0.85, L ~0.97 cold.
- **Kill bar:** W <= 0.90 cold on main + #5374, AND L/M neutral (<= 1.010).
- **W3d:** K1-K9, IR1, IR3, IR4, IR5, IR7, IR8, IR9, T1/T2. New implementation row. E1 reviews the Windows seam.
- **Differential:** the full K9 set:
  - planted links at dir/intermediate/leaf;
  - W junction + 8.3 root + case-variant root;
  - in-root link;
  - mid-install script;
  - warm planted leaf;
  - hardlinked leaf;
  - old `apm_modules` link;
  - git checkout with a tracked symlink.
  Each gives today's verdict, lockfile and receipts on L and W.

### Car 4: B, bulk skills (skill bundle as one staged tree op)

- **Measured ceiling:**
  - L: no gain on top of A+C (skill tree 0.899 vs 0.903 [45528038721 / 45528060785]);
  - W: the journal-off component alone is ~0 [45525632628].
- **Expected faithful:** ~1.00. IR6 forbids the directory copytree the ceiling used, so the faithful car does strictly more work than its ceiling.
- **Kill bar:** >= 3% cold on some OS, else do not build. RECOMMENDATION: drop as a standalone car. Any skill-path gain arrives through Car 5 (manifest-driven, per-file stage from the manifest set).
- **W3d (if built):** IR6, MS3, MS4, MD3.

### Car 5: MD, manifest-driven deploy (the architecture)

- **Scope.**
  - The in-process fetch emits a per-package manifest (path, mode, content hash == pinned blob id, primitive class from the single classifier).
  - Car 2 materializes from it; integrate consumes it, with no re-walk, re-hash or re-classify.
  - Warm re-derives it from pinned tree objects (MS6) + `verify_locked_package`; never a trusted cached manifest.
  - MD5: an unprovable package takes today's whole-package path.
- **Measured ceiling (STACKED, one head, unsafe upper bounds):** L 0.701 vs main [45530064444]; W 0.568 vs main+#5374 [45532025700]; M 0.663 vs main [45530082924]. No paper sums.
- **Charter native-core gate:** "native only if the post-manifest L profile shows >= 25% of CPU in walk/stat/hash/copy". Evaluated on the stacked L profile.
- **Kill bar:** install-cold t50 on 9V74: W <= 7.5 s, and L at the ~4.3 s milestone (regent ruling). The L North Star of 3.5 s is pursued via Lane N.
- **W3d:** MD1-MD5 (use the amended sentence verbatim), MS1-MS7, IR2/IR9 (add the manifest proof owner to the allow-list: gap 5.2-2), T1/T2.
- **Differential:** everything from Cars 1-3, plus class(manifest paths) == today's classification on L/W/M (MD4), and the MD5 whole-package fallback cells.

### Owners (VP-accepted 03:10; work starts after the doc lands WED 12:00 CET)

| car | owner | reviewers / notes |
|---|---|---|
| Car 1 (A plan-once) | E2 | W3d implementation row |
| Car 2 (M2 materialize-once) | E1 | W3d MS1-MS7 row |
| Car 3 (C containment by construction) | PERF-BETS | E1 reviews W; W3d K/IR rows |
| Car 4 (B bulk skills) | DROPPED (VP-accepted) | folded into Car 5 |
| Car 5 (MD manifest-driven deploy) | PERF-ARCH Lead (architect) + L5 (builder, after #5418) | W3d MD1-MD5 |

Precondition: W3d rules gaps G1-G4 (section 3.2) before Cars 3 and 5 start.

### Order and dependencies

```
Car 1 (A) ----------------------------+
Car 2 (M2) --> by-construction trees --+--> Car 3 (C) --> Car 5 (MD)
                                      (Car 4 B: dropped as standalone; folded into Car 5)
```

- Cars 1 and 2 are independent and can land in parallel. Car 3 builds on Car 2's landings (IR1 scope); without them it still lands, but with the I2 walk for every tree.
- Car 5 needs 1, 2 and 3.
- #5374 (W held dirs) and #5382 (K on macOS) are prerequisites already in the v0.39 train.

### Evidence index

- Ceilings: section 2.3.
- Differential harness: `perf-research/arch-render-diff`.
- Ceiling heads:
  - main: 576929fa, 532d7a1f, 893b7b89, ea46241b, 4c98c85e, ea0ec45e;
  - main + #5374: 1b0a0ead, f91d724d, 8efedece.
- Base heads: cc5aac78 (main + #5374).

LANE N (research; regent ruling via PERF VP 03:25 CET):
| lane | scope | deliverable | end date | owner | ship |
|---|---|---|---|---|---|
| N | native filesystem-op core: ONE hot loop (walk/stat/realpath/copy) behind the same W3d invariants (IR/MS/MD) | measured upper bound, L/W/M cold t50, vs the stacked Python ceiling | FRI 27 Nov 03:30 CET (48 h); no bound by then = killed | SoL (2ec55fc5) under the research lead; starts after SoL's doc sections are in | CEO gate: only if >= 0.5 s on L, default-on, across all 3 OS. Then a decision memo: cost, QL/security supply-chain review, wheel matrix |

Lane N method (approved 03:50 CET; plan: SoL lane-n-plan.md):
- Zero-cost ORACLES FIRST, on the stacked head, before any native code:
  - O-walk: one walk per package tree per invocation, served to every caller;
  - O-resolve: the remaining resolve callers made lexical, with the override placed after realpath.py's win32 block.
- Kill rules:
  - KILL Wednesday if both oracles are < 0.5 s L at 9V74.
  - KILL if the native form reaches < 50% of its oracle.
- An oracle that clears is ALSO reported as a Python car (an extension of Car 1). The native lane only has value beyond the oracle's Python form.
- Native shape: returns DATA only; policy stays in the Python owners. Proved by a side-by-side differential build. W3d ruling before any code.

### Replacement North Star research: interpreter + import deletion (FINDING, 04:49 CET; VP-verified)
- **MEASURED ON c2 HEADS (07:40 CET; Bets' clean re-measure on b50d8097 is not folded into this doc, per the CEO 07:55 scope freeze):** every row in this subsection (A3 fork, B, COMBINED) and the deletion map below ran on c2 heads (d3881480 / 6d67f5aa), which skip globbed primitives (nlink guard). Fork == sub digests compared two c2 runs, not base. A3's absolute import deletion (-0.55..-0.82 s L) should transfer; its RATIO and the B/COMBINED/deletion-map rows are suspect. Direction-safe: without c2 L is higher, so 'no measured path to 3.5 s' only strengthens. The M2 6-pair control confirms the M2 kill: L 0.960 / W 0.975 [45560564083 / 45560599802].
- Fork-server oracle (a parent pre-imports all apm_cli + deps, then forks one child per cold install; only the child's wait4 CPU is measured), stacked L/M head d3881480, tree-50, 6 pairs per shard, 3 shards. Rows are `| ORACLE |` lines in the "Lane-N replacement probe (research)" step (harness perf-research/arch-lanen-probe3 @ 3655346e):
  - L [45541271380]: 0.826 (4.561 -> 3.744 s) / 0.807 (3.152 -> 2.551) / 0.806 (2.894 -> 2.346); median 0.807; per-shard absolute -0.55..-0.82 s.
  - M [45541286025]: 0.862 / 0.897 / 0.883; median 0.883.
  - W: no fork; import-only CPU minus bare python 0.625 s (~0.90), a PROXY, not a paired deletion.
  - Faithfulness: identical lockfile digest (af1dced3c0ad) and deployed-file digest (9680bf021f7c, path + mode + bytes) fork vs subprocess, with one identical env dict.
- At 9V74, "~3.47 s" L = 4.3 s (stacked UNSAFE upper bound) x 0.807 (another upper bound): a bound on a bound, not a projection. Shard bases differ (2.9-4.6 s CPU), so the median ratio and per-shard absolutes are quoted.
- Lazy imports were 1.00x (#4812); reaching this needs a process-model change (warm resident process / fork server).
- COMBINED head (result-level pass memos B + fork) [45542197103], L, 3 shards x 6 pairs, 3-way interleave per pair in one VM, identical lock/deployed digests on all sides (`| ORACLE |` rows, harness perf-research/arch-lanen-probe4):
  - COMBINED B+fork: 0.792 / 0.798 / 0.776, median 0.792 -> ~3.41 s ratio-scaled, 3.55-3.67 s absolute at 9V74 (a bound on a bound).
  - B alone: 0.964 / 0.960 / 0.983 (wrapper 0.984) -> ~4.2 s.
  - M B alone 0.911 median, but the wrapper reads 1.011 [45542213046]: noise-grade.
  - W B alone 0.968 median (0.968 / 0.907 / 1.001; wrapper 0.985) [45542228941]: bound <= 4%, same as L.
- VERDICT (VP 04:57, regent informed): MISSES the 3.3 s memo bar. No CEO memo, no resident-process lane, and fork is not a v0.41 ask. B at 2-4% fails the bar: it folds into Car 1 only if it is free (same code paths, no new invariants); otherwise it is dead.
  - B triage (Bets, perf-research/bets-notes @ 64beda00, design-car1-ext-passmemo.md): FREE, so folded into Car 1 by construction: PackageFormatDetector.detect (default detectors) + find_plugin_json + _collect_nested_skill_dirs, read from the per-package plan (~2,550 stats on L; covered by Car 1's own paired read). DEAD (each needs a new invariant): cross-caller hash-pass reuse, _package_files per root (tree-unchanged), compute_file_hash per path (file-unchanged + source/deployed split), _skill_names_in_directory (also used on deploy targets), carried_license._skill_dirs (no in-owner redundancy: 517/517).
- Banked reference: W3d resident-server design (rows w3d:design:resident-server, :fork): spawn-ahead single-use workers on all OS (a LATENCY win; CPU-neutral on our CPU-p50 yardstick, since imports are still paid per command); fork is a Linux-only CPU win (macOS rejected), with F1-F6 pre-fork re-checks; multi-request-in-one-interpreter rejected. Precondition FS0 (import initializes nothing) is filed as #5420 for v0.40 regardless.
- HONEST FINDING: there is no measured Python path to L <= 3.5 s at 9V74. 3.5 s stays the North Star, labelled "no measured path; v0.41+ research". One time-boxed deletion map of the remaining cost (stacked + B head, 60 min) looks for a single owner >= 0.5 s L; if none exists, this is the honest end of the v0.40 research.

## 8. What did not work (kills, with numbers)
- lazy imports (L 0.988 / W 0.995);
- frozen/zipapp boot (no win);
- single-pass integrate (memo engaged, L 0.993);
- records batching (knock-on; ~3% W);
- #5414 locked-warm routing (2.2x worse on main);
- stat-cut beyond form B (~1-2% W);
- frozen + MCP decline (trace artifact);
- warm cache-hit remainder (~0.5 s W);
- "lock-phase 21-23%" (confound).
- Lane N zero-cost oracles (stacked heads, cold t50, 6 pairs, 18/18 valid; engagement proven per OS by served counts, trace 45540082415; on W resolve_path = realpath.py:235, the override after the win32 block, and the native _resolve_windows was called 0 times):
  - O-resolve (every Path.resolve / os.path.realpath lexical): L 0.956 [45534288568] / W 0.954 [45534253181] / M 0.916 [45534321911]; ~0.19 s L at 9V74.
  - O-walk2 (os.walk / scandir / listdir / glob / rglob served at zero cost in integrate): L 0.992 [45538269139] / W 0.987 [45538289388] / M 1.026 [45538306906]; ~0.03 s L at 9V74.
  - Both are below the 0.5 s L bar.
  - Re-measured on CLEAN no-c2 heads (output-audited): O-walk2 L 1.005 / W 0.992 [45562019449 / 45562066339]; O-resolve L 0.969 (~0.17 s at 9V74) / W 0.970 (~0.22 s) [45562045355 / 45562104590]. The kill holds on clean heads.
  - O-TIME whole-run absolute bound [45543605433] (per-thread CPU INSIDE every walk/scandir/listdir/glob/rglob/iterdir/stat/lstat/exists/is_* primitive, 3 runs per OS, stacked head):
    - L 0.48-0.50 s of a 2.7-3.7 s process (13-18%); ~80k calls from ~100 call sites; the largest single family is lstat (realpath chains) at 0.09 s / 27.9k calls.
    - M 1.02-1.27 s (16-19%); W 1.53-1.72 s (19-27%).
    - A native layer still pays the syscalls (~0.15 s on L). So even moving ALL ~80k calls frees at most ~0.4-0.55 s L at 9V74, and that is a ~100-call-site rewrite, not one hot loop. Any SINGLE loop is <= ~0.25 s at 9V74.
  - **Lane N KILLED, WED 25 Nov 05:12 CET, inside its window:** one native fs loop cannot reach >= 0.5 s on L.
- DELETION MAP of the remaining L cost (stacked + B 10720dee; profile [45544853504], time-boxed 60 min; engagement [45546229005]). Paired deletion, L cold t50, 6 pairs vs B, 18/18 valid (here the wrapper row IS the deletion, since head = stub and base = B):
  - prompts+commands family deleted (c970dabc): 1.005 [45546098738];
  - MCP/LSP services phase deleted (5c2a9967): 0.989 [45546121424];
  - post-fetch route_agent_plugin_package + carry_licenses deleted (ac71b23b): 0.988 [45546139343].
  - The best single owner is ~1.2% = ~0.05 s L at 9V74. Inclusive profile shares (e.g. prompt_integrator 11.8%) hosted shared per-package work and freed ~0 when deleted.
  - What remains: the network floor (auth/ref listing ~17%), one write per file, imports (sized by fork: 0.807), and per-file Python spread over ~100 call sites.
  - Clean re-run of the prompts+commands row on a no-c2 B head (0e4d469d, which itself passes the audit; B vs ea0ec45e = 0.982 [45561308612]): 0.960 (~0.21 s at 9V74) [45561291006]. Still below the bar.
  - **No third bet: the honest end of v0.40 research.** L <= 3.5 s stays the North Star with no measured path; v0.41+ research.
  - Kept as a candidate, not a lane: on W the whole fs-primitive layer is ~1.2-1.6 s at 9V74 (Windows stat/exists cost 2-3x more). That is a possible future W-only many-call-site redesign. W is already at target, so it is not a v0.40 item.
- **Lessons:**
  - "a ceiling stub must produce byte-identical deployed output; c2 was fast because it skipped deploys (nlink guard)" (VP 07:32).
  - Every ceiling/oracle head must pass a deployed-file digest equality check vs its base (path + mode + bytes). c2 (hardlink materialize) silently skipped globbed primitives (st_nlink > 1 guard) and overstated the stacked ceiling; it was never digest-checked. The A3 fork probe was (identical digests), which is why its rows stand.
  - attribution ranks, deletion sizes; 45% fs leaf-kind -> ~5% deletable. The 45% native-core gate counted fs-NAMED Python frames, not syscall CPU:
    - kinds are mapped by frame-name substring (":resolve", ":read", ":stat", ":walk"...), which catches abspath (the ceiling's own lexical replacement), click's resolve, read_manifest_text, etc.;
    - a leaf is the innermost Python frame including its own bytecode (pathlib wrappers inside per-file caller loops that a native walk does not remove);
    - file_io is needed bytes and copy machinery (M2's lane);
    - the sampler charges per-thread CPU deltas to the frame on top, which biases toward long-lived generator/wrapper frames.
  - New rule: native only if a zero-cost oracle frees >= the bar.
  - re-measure ceilings on current main;
  - profiles rank, paired deletion sizes;
  - every headline claim needs one real-network confirmation (S88);
  - Windows has its own resolve_path (realpath.py redefines it under sys.platform == "win32"), so any lexical/stub ceiling on realpath must override AFTER the platform block, or it silently does nothing on W. The first stacked W read (0.719) was invalid for this reason. The 2026-11-24 containment ceiling 365324c6 W 0.748 may also be partial on W.

## 9. Evidence index
Run IDs appear inline in every table row; the per-section evidence indexes are in sections 1, 2 and 7.

## Side finding
- #5418: the lockfile embeds absolute paths for monorepo sibling packages. Pre-existing; v0.40 with a rekey.
