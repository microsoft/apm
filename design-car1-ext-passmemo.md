## Lane-N B (result-level pass dedup): fold into Car 1 only where free, otherwise DEAD

**Measured.** All eight memos together give:
- L: paired 0.964 median across 3 shards (0.964 / 0.960 / 0.983), run 45542197103; wrapper paired CPU 0.984.
- M: 0.911, run 45542213046 (wrapper 1.011, noisy).
- W: run 45542228941 pending (for the record only).

Setup: tree-50, 6 pairs per shard; heads L/M 10720dee and W ec0f9e4c; harness probe4 b22f8968. Digests are identical to base: lock af1dced3c0ad, deployed 9680bf021f7c.

At 9V74 that is about -0.1 s on L 4.3 s.

**Test.** A memo is FREE when Car 1 (classify / plan-once) already computes the value once and threads it to the consumer: the same code path, no new invariant. A memo NEEDS A NEW INVARIANT when reuse across call sites requires "the tree did not change between the two observations".

FREE: fold into Car 1 by construction, since they are classification inputs Car 1 already computes once per package:
- `PackageFormatDetector.detect` (default detectors), with `find_plugin_json` and `_collect_nested_skill_dirs` beneath it.
- These are the "detect <- detect" / "find_plugin_json <- detect" owners in the attribution (1350 + 600 + 600 stats on L).
- Car 1's plan already carries the classification. Consumers read it from the plan instead of re-detecting, so there is no cache, no lifetime and no invalidation.
- Expected share: a minority of the 0.964. Not measured separately; Car 1's own paired read covers it.

DEAD: each needs a new invariant.
1. **One hash pass, both streams, reused across callers** (`_compute_package_hash_impl` vs `compute_package_digests`). Computing both streams in one call is free, but the win comes from the SECOND caller reusing the first's result. That needs "no write to the package root between the two hashes". Carried-license copies and re-materialization write there, so this is a new receipt-keyed invariant.
2. **`_package_files` per root.** Same tree-unchanged invariant.
3. **`compute_file_hash` per path.** File-unchanged invariant, plus a new source/deployed split rule: deployed-side hashes must never be reused.
4. **`SkillIntegrator._skill_names_in_directory`.** Also called on PROJECT target dirs, which change as skills deploy, so it needs a source-only split and the tree-unchanged invariant.
5. **`carried_license._skill_dirs`.** No intra-owner redundancy (517 calls over 517 distinct dirs). Reuse would mean sharing a walk with `compilation/inventory.collect`, which is a cross-owner inventory: a new invariant. And SoL's O-walk2 already bounds walk sharing at L 0.992.

**Verdict.** Fold the FREE subset (format detection and its helpers) into Car 1 as consumers of the plan. Everything else is DEAD; no new invariants for a <= 4% L bound.
