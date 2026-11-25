# v0.40.0 "5x of floor" design doc -- OUTLINE (due WED 12:00 CET; owner PERF-ARCH d7d7be0d)
Target: install-cold t50 on 9V74: L <= 3.5 s, W <= 7.5 s, default-on, 0 errors.
1. Physics floor per phase vs today, RE-GROUNDED on main 1e4a208e (02:22): integrate W 50% / L 45% of cold (0.499/0.551), ~48% of warm; lockfile+ledger+journal W 21% / L 23%; policy/audit 2-4%; residual ~25% (boot + resolve + fetch + glue). Profiles: W containment 6.2 s of 16.8 s; L flat (rest/containment/writes ~1.2-1.3 s each). Family re-splits on main ETA 03:00.
2. HEADLINE: integrate redesign (main: W 50% / L 45% of cold; table phase-map-main.md). Families W 31% (skills 14.6% / 1.9 s = #1, instructions 7.3%, prompts 5.6%, agents 3.8%); shared per-file pipeline W 18.8% / L 19.6% (records 3-9%, skill preflight 4%). Next-biggest non-integrate block: lockfile+ledger+journal 21-23% (E2). Cost is per-file Python + containment (W profile containment 6.2 of 16.8 s), not bytes.
3. Records block re-sized ~3% W / ~5% L (0.905 overstated). Lockfile/ledger/journal phase (E2): ceiling 1.6 s L / 3.1 s W; bar >= 30% by WED 12:00.
4. Warm/locked: on current main the diff-apply engine is ~2.2x cheaper than full pipeline; locked-warm L 4.3 s < warm 7.8 s; regression GONE (#5414 killed, 2.22x worse on main [45517792540], trace [45519638267]). Warm cache-hit remainder ls-tree ~0.5 s W [45515035586/45515126517].
5. W3d surface: invariants per phase (containment K1-K9, integrity, receipts, policy) + fastest faithful design + cost lines.
6. Evidence index: run IDs.
Kill bars: integrate <= 50% of today by paired deletion within 24 h of doc, else kill/re-plan.
