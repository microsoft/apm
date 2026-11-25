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
| MCP/LSP services reconcile | 6.8% / 3.4% (0.32 / 0.53 s) | 0.4 / 0.45 | stack: 0.996 / 1.002 (F19) | ~0.01 | 0.4 / 0.45 | 6 MCP servers: one config merge + write per runtime. PROBE vs DELETION DISAGREE; verify on main |
| glue (pipeline setup, session construction, exit) | 4-5% (0.2 / 0.7 s) | 0.3 / 0.65 | n/a | ~0.02 | 0.3 / 0.6 | |
| **total** | 4.68 / 15.8 s on the probe runner | **6.1 / 13.2** | | **~0.7 / ~1.2** | **~5.4 / ~12** | measured/floor = **~8.7x L, ~11x W** |

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
   Network bytes and request counts are already at floor (101 requests, 0.6 MB). The gap is per-file
   Python on fetch-write and checkout-copy: a CAS store + link materialization would collapse the two
   writes per file into one link.
3. **Lock + ledger + journal: no lock-phase lever.** The lockfile build itself is 0.14 / 0.39 s (~3%). The
   21-23% deletion is a confound: without a lockfile, downstream phases skip their work. The real
   costs sit inside integrate: the ledger's _trusted_hash lstat chain (~3,300 lstat, W ~0.9 s; a
   containment-family cost, see section 2) and the deploy journal (L ~7%). The records split (F20) found
   no byte-identical batching that reaches 5%. This needs a records-model redesign (per-package records),
   the v0.40 headline.
4. **Boot is no longer negligible on L** (~10% of cold). Imports are flat at ~0.4-0.5 s while cold shrank,
   but lazy imports measured nothing (F15). The floor needs fewer modules on the install path.
5. **Policy/audit/scan and reachability are small** (<= 4%). Not design-doc levers.
6. **MCP services** is an open discrepancy: the probe attributes 0.3-0.5 s to the services phase, while
   the stack deletion read ~0. It could be a background thread (update check, thread-pool teardown)
   running during that phase. One paired deletion on main settles it.

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
