# Integrate redesign: candidate shapes for "integrate CPU <= 50% of today"

Base: main 1e4a208e. Fixture tree-50, install-cold.

## Cost map (measured)

- Integrate share of cold: W 0.499 when integrate is skipped (SoL skip-integrate run 45520602660); L ~45%.
- L cProfile of main, run 45523400455 (11.05 s profiled; `integrate.run` 5.21 s = 47%):

| family | calls | cum s | note |
|---|---|---|---|
| `run_integration_template` | 50 | 3.56 | per-package pipeline |
| `prepare_integration_materialization` | 50 | 0.82 | |
| `preflight_skill_materializations` | 1 | 0.61 | |
| `discover_primitives` | 250 | 1.01 | 5 integrators x 50; memo hit after 50 misses, but each miss walks the tree 3x (`find_primitive_files` 150 = 3 types x 50, one `CompileInventory.collect` each) |
| `detect_package_type` / `format_detection.detect` | 250 | 1.00 / 0.97 | re-detected per integrator |
| `validate_apm_package` | 100 | 1.07 | 2x per package |
| `compute_package_hash` / `_package_files` | 100 / 150 | 1.07 / 1.43 | 2x per package |
| `init_link_resolver` | 250 | 1.07 | per integrator; 1000 `resolve_path` calls |
| `from_apm_yml` | 251 | 0.50 | |
| `_copy_skill_bundle` + `copy_directory_files` | 100 / 100 | 0.95 / 0.63 | per-file copy pipeline |
| write journal (`record_before/after_write`, `deploy_snapshot._event`) | ~900 writes | ~0.7 | per file |
| `resolve_path` (containment) | 25,233 | 0.78 L | W profile: containment 6.2 of 16.8 s |

- W ranking (lead's profile): containment 6.2 s, writes 2.3 s, discovery 1.7 s, hash 1.0 s.
- Containment call count scales with the re-walks above. On W each `resolve_path` costs a `CreateFileW` (main, before #5374).

## Shape A: per-package plan computed once (RANK 1)

- **Hypothesis.** Integrate re-derives the same per-package facts 2-5x: type, primitive inventory, parsed `apm.yml`, link-resolver contexts and content hash. Compute them once, in one `PackagePlan` per package at materialization, and have every integrator consume the plan.
- **Deletes.** 4/5 of detection and link-resolver scans; 2/3 of the inventory walks; 1/2 of validation and hashing. The containment calls those walks issue go with them, which is what matters on W.
- **Paper ceiling.**
  - L: integrate 5.2 s -> ~2.9 s (detect -0.8, walks -0.55, validate -0.5, hash -0.5, link -0.4, yml -0.4, minus overlaps). About 55% of today.
  - W: containment calls fall roughly in proportion (discovery and link resolution are the top resolve_path callers), and integrate falls toward ~50%.
  - MEASURED upper bound of the memo part (`perf-research/arch-integrate-plan-once` @ 576929fa: detect/validate/hash memoized, one inventory per package), cold t50 6 pairs vs main 1e4a208e: W 0.970 (16.68 -> 15.77 s, run 45524106186), L 0.976 (7.26 -> 7.11 s, run 45524085463). That is -0.9 s W (-14% of W integrate 6.5 s) and -0.15 s L (-6% of L integrate 2.7 s); cProfile overstated these families ~2x. ALONE IT DOES NOT REACH THE BAR (needs -3.3 s W / -1.35 s L).
- **Cost line.** Per package: 1 tree walk (was 3), 1 detect (was 5), 1 hash (was 2), 1 `apm.yml` parse (was 5). Spawns 0; syscalls about -60% on the walk path.
- **W3d invariants.**
  - The plan is valid only for the tree it read. Invalidation rides the same seams as K (R2/R4 `release_held` -> forget). User code mid-command drops it (`forget_invocation_memos`). The plan lives in `invocation_memo`.
  - K1-K9 unchanged: writes still go through `write_seam` with per-file containment at the destination.
  - Receipts: per-file deployed hashes are still computed from the bytes written. Ownership is unchanged.
- **Faithful design.** Thread one `PackagePlan` through `run_integration_template` (owner: `install/template.py`). Integrators take `plan.primitives`, `plan.type` and `plan.link_contexts`, and no longer call discovery themselves. An arch guard stops integrators calling `discover_primitives`/`detect_package_type` directly.

## Shape B: skills deployed as one tree op (RANK 2)

- **Hypothesis.** A skill bundle is copied file by file through the copy and journal pipeline (`_copy_skill_bundle` 0.95 s plus a share of the 0.7 s journal). Instead, deploy each bundle as one staged tree: reflink copytree into a staging dir, one tree hash, one atomic rename into place, one journal unit of kind "dir".
- **Deletes.** Per-file journal records (~900 -> ~50 + non-skill files), per-file containment at the destination (one proof per new directory, which is by construction because APM creates it), and per-file `record_before/after_write`.
- **Paper ceiling.** L about -1.0 s of integrate. W more, since destination containment and writes (2.3 s) shrink.
- **Cost line.** Per skill: 1 copytree + 1 rename + 1 tree hash (was N file writes + 2N journal events + N containment checks).
- **W3d invariants.**
  - K2: a tree created this invocation is proven by construction. K5: never write in place; the staged tree plus atomic replace keeps that.
  - Receipts: the lockfile still lists each deployed file with its hash, computed from the staged tree in one pass.
  - Ownership: the destination directory must be absent or APM-owned (ledger) before the swap.
  - Deploy journal: a "dir" unit with `_tree_hashes` already exists in `deploy_snapshot`.

## Shape C: containment by deploy root (RANK 3)

- **Hypothesis.** Prove each target deploy root (`.github/`, `.claude/`, ...) once per command, check writes under it lexically, and refuse a link only at the leaf.
- This is lever K applied to destinations. Measured: on main without #5374, K gave W 0.842. On top of #5374, Windows was neutral, so K now ships macOS only (#5382). The W containment lever is #5374 itself, already in the train.
- **Paper ceiling on main.** W -16% of cold (K, measured), L ~0. Overlaps with #5374, so it is not additive.
- **W3d invariants.** K1-K9 (ruled); R1-R4 and the clear-all forget as in #5382.

## Shape D (folds into A): discovery from the fetch manifest

- The in-process fetch already returns `{path: (blob id, exec)}` for every file it writes. Discovery and the file list for hashing could read that manifest instead of walking the tree.
- Deletes the remaining walk per package. Hash-on-fetch would remove `compute_package_hash` entirely (sha256 computed as bytes are written).
- **Invariant.** The manifest is trusted only for trees this invocation wrote (as `register_inprocess_manifest` already is for verify).

## Pick

A first. It is the largest measured family, it shrinks containment call counts on W without new trust, and its invariants reuse K's seams. Then B. C stays on #5374 plus #5382.

## Measured ceilings (main 1e4a208e, tree-50 cold, 6 pairs, paired CPU)

| head | W | L |
|---|---|---|
| A memo only (576929fa) | 0.970, -0.9 s [45524106186] | 0.976, -0.15 s [45524085463] |
| A + journal off ONLY (532d7a1f) | 0.969 [45525632628] | 0.936 [45525615896] |
| A + lexical resolve / no link scan ONLY (893b7b89) | 0.752 [45525667777] | 0.947 [45525650620] |
| A + both, full ceiling (ea46241b) | 0.688, -5.8 s [45524926467] | 0.901, -0.54 s [45524908282] |

- W clears the bar (-3.3 s); L misses it (-1.35 s).
- On W the win is containment: redesign it as by-construction proofs for APM-created destination trees plus one resolve per deploy root. Do not remove it. The journal costs ~0 on W and ~4% on L.
- L's remainder is content transforms: frontmatter re-parsed ~19x per package (950 calls), write_text_lf, and copies. Parse once per file in the plan, render per target, and land skills as one tree op.
