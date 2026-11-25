# Design doc, section 1: the physics floor per phase, on main (PERF-SOL, 25 Nov 2026)

Scope: `apm install`, cold (no lockfile, empty cache), tree-50 fixture (50 packages, 89 edges, 11 primitives
per package, MCP server in every 8th package), anonymous, main 1e4a208e. All numbers are CPU (user+sys,
apm plus children) measured on CI. Nothing was measured locally.

## How each column was measured
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

## Unit costs used (hosted runners, process CPU)
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

## The table (cold t50, main)

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

## Materialization re-measured on main (resolve+fetch row): the copy into apm_modules
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

## Reading
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

## Caveats
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

## Stacked ceiling: the doc's headline number (measured on one head, not a paper sum)
Head = Bets' integrate ceiling (perf-research/arch-integrate-ceiling-skilltree ea0ec45e: main + A+B +
parse-once + skill tree as one op; UNSAFE upper bound) + my M2 bound c2 (hardlink, no re-verify; UNSAFE).
L/M: perf-research/arch-stack-ceiling-L d3881480 vs main 1e4a208e. W: perf-research/arch-stack-ceiling-W
2a69add2 vs its base cc5aac78 (main + #5374), default schannel world. Cold t50, 6 pairs, anon, 0 errors.
| OS | paired ratio | base -> new (bench runner) | ~s at 9V74 | target | run |
|---|---|---|---|---|---|
| L | 0.701 | 7.18 -> 5.03 s | 6.1 x 0.701 = **4.3 s** | 3.5 s (miss by ~0.8 s) | 45530064444 |
| W | **0.568** (vs main+#5374; head W2 6d67f5aa) | 15.09 -> 8.44 s | schannel calibration [45534359514]: base 10.53 s at 9V74 x 0.568 = **~6.0 s** (9V45 4.5, Xeon 8573C 6.4, 7763 8.3-8.6) | 7.5 s: AT TARGET on 3 of 4 classes | 45532025700 |
| M | 0.663 | 8.18 -> 5.37 s | n/a (no 9V74 class) | - | 45530082924 |
W correction: the first W head (2a69add2, 0.719) lacked the lexical-resolve ceiling on Windows, because
realpath.py redefines resolve_path inside `if sys.platform == "win32":` and the L ceiling's stub sat in
the generic function. W2 6d67f5aa = Bets' W ceiling 8efedece (lexical override at end of file) + c2 +
parse-once + skill tree. Caveat: the 13.2 s W reference is the OpenSSL-world calibration (F16); the default
schannel world was then calibrated directly (run 45534359514: main+#5374 cold t50 = 10.53 s at 9V74), so W is ~6.0 s at 9V74.
Overlap vs paper (L): c2 alone 0.817, ceiling alone 0.899 (Bets 45528038721). Paper additive = 0.716,
multiplicative = 0.734; measured 0.701. They stack slightly better than the paper (removing the copy also
removes integrate-side walks of the copied tree). Per-shard ratios within +-1.5%.

## Native-core gate (charter: "native only if the profile shows >= 25% of CPU in walk/stat/hash/copy")
cpu_attr leaf kinds, L process CPU, one profiled sample each:
| head | walk_scan | stat | hash | file_io (copy/read/write) | gate sum | + realpath | run |
|---|---|---|---|---|---|---|---|
| main 1e4a208e | 10.7% | 14.6% | 0.2% | 19.9% | **45.5%** | 54.2% | 45520769932 |
| stacked ceiling d3881480 | 18.1% | 11.6% | 0.2% | 15.3% | **45.2%** | 65.1% | 45530101839 |
**Gate MET on main and at the stacked head (45%, >= 25%).** Hash is negligible (0.2%): bytes are not the
cost. The native-core candidate is the walk/stat/realpath/file-op layer (syscall-heavy Python), not hashing.
Even at the stacked ceiling, 65% of L CPU is filesystem-syscall Python, plus 13% imports.
