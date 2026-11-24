# v0.40.0 "5x of floor" design doc -- OUTLINE (due WED 12:00 CET; owner PERF-ARCH d7d7be0d)
Target: install-cold t50 on 9V74: L <= 3.5 s, W <= 7.5 s, default-on, 0 errors.
1. Physics floor per phase (bytes, hashes, round-trips, spawns, syscalls) vs today (paired-deletion map, SoL 20:09: integrate 60% L / 58% W; lockfile+ledger+journal 27%; policy 3%; residual <=10%).
2. Integrate redesign: per-file Python + containment (not bytes; single-pass killed F18). Inputs: 22:30 per-family split; K/#5374 overlap; per-file call counts.
3. Lockfile/ledger/journal phase (E2): ceiling 1.6 s L / 3.1 s W; bar >= 30% by WED 12:00.
4. Warm/locked regression (locked-warm/warm 1.21-1.29): attribution 21:30.
5. W3d surface: invariants per phase (containment K1-K9, integrity, receipts, policy) + fastest faithful design + cost lines.
6. Evidence index: run IDs.
Kill bars: integrate <= 50% of today by paired deletion within 24 h of doc, else kill/re-plan.
