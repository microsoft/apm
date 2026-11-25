# v0.40.0 "5x of floor" design doc -- OUTLINE (due WED 12:00 CET; owner PERF-ARCH d7d7be0d)
Target: install-cold t50 on 9V74: L <= 3.5 s, W <= 7.5 s, default-on, 0 errors.
1. Physics floor per phase vs today -- RE-GROUNDING on main 1e4a208e (SoL by 04:30); stack e62f520f map (integrate 60% L / 58% W; lockfile+ledger+journal 27%; policy 3%) is stale.
2. HEADLINE: integrate redesign (60% L / 58% W). F19 split: shared per-package pipeline W 3.1 / L 1.9 s; skills W 1.9 / L 0.8 s (4-5x a file primitive); file primitives ~1.7 s W spread. Records model (fewer, coarser records) is a sub-part, re-sized ~3% W / ~5% L. Cost is per-file Python + containment, not bytes (single-pass killed F18); K/#5374 overlap on W.
3. Records block re-sized ~3% W / ~5% L (0.905 overstated). Lockfile/ledger/journal phase (E2): ceiling 1.6 s L / 3.1 s W; bar >= 30% by WED 12:00.
4. Warm/locked: on current main the diff-apply engine is ~2.2x cheaper than full pipeline; locked-warm L 4.3 s < warm 7.8 s; regression GONE (#5414 killed, 2.22x worse on main [45517792540], trace [45519638267]). Warm cache-hit remainder ls-tree ~0.5 s W [45515035586/45515126517].
5. W3d surface: invariants per phase (containment K1-K9, integrity, receipts, policy) + fastest faithful design + cost lines.
6. Evidence index: run IDs.
Kill bars: integrate <= 50% of today by paired deletion within 24 h of doc, else kill/re-plan.
