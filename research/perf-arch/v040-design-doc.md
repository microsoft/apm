# APM v0.40.0 -- "5x of floor" install architecture (DRAFT)

Owner: [PERF] Lead, Perf Architecture Research. Co-signers: W3d (security), PERF VP.
Status: DRAFT, assembled 2026-11-25. Due WED 12:00 CET.
Scope: `apm install` cold fresh, warm and locked journeys at tree-50 (50 deps, 684 deployed files).

## 0. Summary

- **Target** (reference runner class EPYC 9V74, cold t50, default-on, 0 errors): Linux <= 3.5 s, Windows <= 7.5 s.
- **Main today (1e4a208e):** Linux 6.1 s, Windows 13.2 s. The physics floor is ~0.7 s Linux / ~1.2 s Windows, so the gap is 8.7x Linux / 11x Windows.
- **Where the gap is** (measured by paired deletion and a per-phase CPU probe on main):
  - integrate is ~45% Linux / ~50% Windows;
  - resolve + fetch is ~33%, of which the second copy + re-verify alone is 15-20% on every OS.
- **Architecture: one manifest-driven deploy.**
  - The in-process fetch emits a per-package manifest (path, mode, content hash, primitive class).
  - Materialization writes each file once (M2), and integrate consumes the manifest, with no re-walk, re-hash or re-classify.
  - Containment is proved by construction for APM-created trees.
- **Result** (stacked measured ceiling): [TBD 04:30, SoL]. Windows clears its target on paper; Linux reaches ~1.17x of its target on paper.
- **Native-core gate on Linux:** [TBD: met / not met (X%)].
- **Linux bar:** the regent decides between the options in section 6.

## 1. Physics floor per phase (on main)
[from SoL design-sec1-floor.md; table: phase share, seconds, floor, gap, method; confound notes]

## 2. Integrate redesign
[from Bets design-sec2-integrate.md]
- **Invariant first** (W3d co-signed 2.1, IR1-IR9).
- **Shapes:**
  - A: plan-once (+ parse-once);
  - B: bulk deploy (staged per regular file);
  - C: containment by construction.
- **Ceilings:**
  - W, main + #5374: 0.718 (-4.45 s).
  - L, A+B+parse+skill trees: 0.899. L residual: classify 1.15, preflight 0.82, acquire 0.75, skill copy 0.73 ms/file.
- **Plain statement:** the integrate redesign alone clears W, not L.

## 3. Materialize-once (M2)
- Today each file is fetched into the cache, copied into apm_modules, and the copy is re-verified (github_downloader.py:1922-1927).
- Deletion bound (c2): cold W 0.802 / L 0.817 / M 0.856; warm W 0.833 / L 0.794.
- The cost is Python per-file copy machinery (~1.4 ms Linux / 3.5 ms Windows per file).
- W3d ruled M1 and M2 OK (MS1-MS7) and M3 hardlinks rejected.

## 4. Manifest-driven deploy
- The manifest is NOT a trust root (W3d MD1-MD5, amended sentence verbatim from row w3d:ruling:manifest-deploy).
- Warm installs re-derive the manifest from pinned tree objects.

## 5. Security surface (consolidated)
[from Bets design-sec3-security.md: K1-K9, IR1-IR9, MS1-MS7, MD1-MD5, with test cells and cost lines]

## 6. Linux bar options (regent's call)
- (a) Keep L <= 3.5 s. This requires removing per-file Python beyond the manifest: a native core, ONLY if the gate is met ("native only if profile shows >= 25% of CPU in walk/stat/hash/copy"; measured [TBD]).
- (b) Revise the L bar to the stacked measured ceiling [TBD].

## 7. Build plan (phased cars, default-on, kill bars)
[from Bets design-sec4-build-plan.md]

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
- **Lessons:**
  - re-measure ceilings on current main;
  - profiles rank, paired deletion sizes;
  - every headline claim needs one real-network confirmation (S88).

## 9. Evidence index
[run IDs per row]

## Side finding
- #5418: the lockfile embeds absolute paths for monorepo sibling packages. Pre-existing; v0.40 with a rekey.
