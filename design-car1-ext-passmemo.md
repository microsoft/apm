## Car 1 extension candidate: result-level pass dedup (lane-N B)

**What it deletes.** Cold fresh t50 walks and hashes each package tree several times, once per owner.
- The owner attribution (L, 55 packages, run 45541158727) shows the content-hash tree pass is the largest:
  - `content_hash._package_files`: 3102 directory listings over 1034 distinct dirs, and 7254 stats.
  - Tree bytes are read 1434 + 717 times: `_compute_package_hash_impl` plus `compute_package_digests`.
- Next come format detection and `find_plugin_json` (1350 stats), then the `skill_integrator` directory scans.
- `apm.yml` parsing is already memoized by text (`manifest_io._parsed_texts`), so its 9x re-reads are only syscalls.

**Ceiling head.** L/M head 10720dee (perf-research/arch-passmemo) on d3881480; W head ec0f9e4c on 6d67f5aa. These are unsafe per-invocation memos, applied only to paths under apm_modules:
1. One package-tree hash pass computes both streams (legacy and framed), memoized per (package, exclude).
2. `_package_files` per (root, native, skip_roots).
3. `compute_file_hash` per path.
4. `find_plugin_json`.
5. `PackageFormatDetector.detect` (default detectors only).
6. `_collect_nested_skill_dirs`.
7. `SkillIntegrator._skill_names_in_directory`.
8. `carried_license._skill_dirs`.

**Measured.**
- Probe B_sub/base_sub (harness perf-research/arch-lanen-probe4 @ b22f8968; tree-50, 6 pairs per shard, 3-way interleave):

  | OS | Run | Shard 1 | Shard 2 | Shard 3 | Median | Wrapper paired CPU |
  |----|-----|---------|---------|---------|--------|--------------------|
  | L | 45542197103 | 0.964 | 0.960 | 0.983 | **0.964** | 0.984 |
  | M | 45542213046 | 0.911 | 0.874 | 0.918 | 0.911 | 1.011 (noisy) |

  W: run 45542228941 pending.
- Faithfulness: lockfile af1dced3c0ad and deployed-file digest 9680bf021f7c are identical to base on every shard.
- At 9V74, L 4.3 s goes to about 4.15-4.2 s (-0.08..-0.15 s). It is a Python car, not a North Star.

**Safety work each memo needs to ship (cost line per row).**
- **Lifetime (IR2).** Every memo lives in `invocation_scope` only and is cleared by `forget_invocation_memos` (lifecycle scripts). The bump uses the same generation discipline as the K proofs (capture the generation before the observation, publish only if unchanged; G3).
  - cost: one int compare per publish.
- **Invalidation on writes.** The apm_modules tree is NOT write-once within an invocation:
  - carried-license copies add files (covered by `exclude`, which callers derive from the download result, never from disk);
  - re-materialize, update and prune rewrite or remove trees.
  - So the hash and file-list memos must be keyed by the materialization event: the M2 materialize-once receipt, (package, sha, variant). Any rewrite of a package root must drop that root's entries. Owner: the materializer, via one `forget_package(root)` hook. Keying by path alone is the unsafe part of this head.
  - cost: one dict pop per materialize.
- **Hash pass (memo 1).** Computing both streams in one pass is safe by construction: same bytes, two hashers.
  - It must still read through the no-follow / containment path (the K4/K-series rules, symlink refusal in `_package_files`).
  - Verification of a fetched package against its pinned hash must use a hash computed AFTER the last write to that root. This follows from the receipt key above.
  - cost: zero extra (one fewer pass).
- **compute_file_hash (memo 3).** Only for sources under apm_modules. Deployed-side hashes (project files, `compute_deployed_hashes`) must never be memoized: they are written during the same invocation, and the deployed-hash provenance (lockfile `deployed_files`) must describe the final bytes.
  - cost: none on the source side.
- **Detection and scans (memos 4-8).** These are pure functions of a package root's tree, so they need the same receipt keying. `_skill_names_in_directory` is also called on PROJECT skill dirs: memoize only the apm_modules branch, never target dirs (they change as skills deploy).
  - cost: none.
- **Not memoized:**
  - The security gate scan (667 reads, single pass; it must see deployed bytes).
  - The policy checks.

**Recommendation.** Fold memos 1-3 (hash pass, file list, source file hash) into Car 1 (plan-once) as a package-tree inventory produced once per materialization receipt and consumed by the hash, detection, discovery and carried-license owners. That is the "visit once" shape. Memos 4-8 become free consumers of that inventory. Expected L about 0.96-0.98 on its own. Kill bar: < 2% L paired at the car head.
