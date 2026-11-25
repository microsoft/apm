# Section 4: v0.40 build plan (phased cars, each landable alone, default-ON)

Rules for every car:
- Default-ON, no flag. A kill switch (env) is allowed only as an escape hatch that keys the verdict record.
- Lands alone on main. Benched at its FINAL head vs its merge-base: cold+warm t50 L/W/M, 6 pairs, paired CPU incl. children.
- Passes the render/materialization differential with zero diffs. The known-variant fields are exactly #5418's until #5418 lands; nothing else.
- Has its W3d row(s) and QL H8 before merge.
- Expected ratios come from MEASURED ceilings (section 2.3 / SoL section 1). A faithful car is expected to keep 50-80% of its ceiling. Kill bars are on the faithful car.

## Car 1: A, plan once per package (classify, inventory, parse once)

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
- **Owner suggestion:** PERF-BETS (it holds the ceilings and the differential harness).

## Car 2: M2, materialize once (fetch into the final tree; cache = objects)

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
- **Owner suggestion:** SoL (owns the c2 measurement) with F&C (B1 landing seam).

## Car 3: C, containment by construction (destinations and sources)

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
- **Owner suggestion:** PERF-BETS (lever K owner) + E1 (Windows Paths).

## Car 4: B, bulk skills (skill bundle as one staged tree op)

- **Measured ceiling:**
  - L: no gain on top of A+C (skill tree 0.899 vs 0.903 [45528038721 / 45528060785]);
  - W: the journal-off component alone is ~0 [45525632628].
- **Expected faithful:** ~1.00. IR6 forbids the directory copytree the ceiling used, so the faithful car does strictly more work than its ceiling.
- **Kill bar:** >= 3% cold on some OS, else do not build. RECOMMENDATION: drop as a standalone car. Any skill-path gain arrives through Car 5 (manifest-driven, per-file stage from the manifest set).
- **W3d (if built):** IR6, MS3, MS4, MD3.

## Car 5: MD, manifest-driven deploy (the architecture)

- **Scope.**
  - The in-process fetch emits a per-package manifest (path, mode, content hash == pinned blob id, primitive class from the single classifier).
  - Car 2 materializes from it; integrate consumes it, with no re-walk, re-hash or re-classify.
  - Warm re-derives it from pinned tree objects (MS6) + `verify_locked_package`; never a trusted cached manifest.
  - MD5: an unprovable package takes today's whole-package path.
- **Measured ceiling:** the STACKED ceiling (Car 2 c2 + integrate ea0ec45e) is being measured by SoL (~04:30). The doc quotes only that number; no paper sums.
- **Charter native-core gate:** "native only if the post-manifest L profile shows >= 25% of CPU in walk/stat/hash/copy". Evaluated on the stacked L profile.
- **Kill bar:** the v0.40 target (install-cold t50 on 9V74: L <= 3.5 s, W <= 7.5 s), or the revised L bar (regent call, both options shown with the stacked number).
- **W3d:** MD1-MD5 (use the amended sentence verbatim), MS1-MS7, IR2/IR9 (add the manifest proof owner to the allow-list: gap 3.2-2), T1/T2.
- **Differential:** everything from Cars 1-3, plus class(manifest paths) == today's classification on L/W/M (MD4), and the MD5 whole-package fallback cells.
- **Owner suggestion:** PERF-ARCH Lead (architecture), with SoL (materialize) and PERF-BETS (integrate consumption).

## Order and dependencies

```
Car 1 (A) ----------------------------+
Car 2 (M2) --> by-construction trees --+--> Car 3 (C) --> Car 5 (MD)
                                      (Car 4 B: dropped as standalone; folded into Car 5)
```

- Cars 1 and 2 are independent and can land in parallel. Car 3 builds on Car 2's landings (IR1 scope); without them it still lands, but with the I2 walk for every tree.
- Car 5 needs 1, 2 and 3.
- #5374 (W held dirs) and #5382 (K on macOS) are prerequisites already in the v0.39 train.

## Evidence index

- Ceilings: section 2.3.
- Differential harness: `perf-research/arch-render-diff`.
- Ceiling heads:
  - main: 576929fa, 532d7a1f, 893b7b89, ea46241b, 4c98c85e, ea0ec45e;
  - main + #5374: 1b0a0ead, f91d724d, 8efedece.
- Base heads: cc5aac78 (main + #5374).
