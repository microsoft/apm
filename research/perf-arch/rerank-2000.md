# 20:00 CET re-rank (PERF-ARCH) -- install-cold t50, anon, reference class EPYC 9V74 (VP 20:02)
Base main c0296fab on 9V74: L 6.1 s, W 13.2 s [45457579415]. Projected v0.39.0 stack (open PRs) ratios L 0.972 / W 0.875 [45446611653/45446661279] -> L 5.9 s, W 11.5 s. Targets L 3.5, W 7.5.
| # | lever | owner | measured | proj. effect (9V74) | status |
|---|---|---|---|---|---|
| 1 | K containment once per root (W3d K1-K9) | Bets | ceiling W 0.748 / L 0.904 [45452685355/45452638560]; faithful est W ~0.82, L ~0.93 | W -2.1 s, L -0.4 s | head 23:00, READY 03:00, kill W<10% |
| 2 | schannel B1 (default GfW population) | F&C | B1 W 0.444 where engaged; main = git path for default W | default-W cold ~2x | building; gate W<=0.60 on JH harness |
| 3 | rest-cut defects (absent-config isfile, scope root re-resolve) | SoL proto -> E2 | paper W -0.5 s (0.36+0.15) | W ~0.96 | benches ~19:55; E2 if >=3% W |
| 4 | portable_relpaths W-gated resolve_path | E1 in #5374 | 0.55 s W self | W -0.5 s | folding |
| 5 | A' listing shared with #5318 reachability | F&C (#5353) | ls-remote x50 1.55 s L warm-auth | warm-auth L ~0.79 | verify memo sharing |
| 6 | lever S #5377 (monorepo subdir deps) | Bets | real fixture L 0.502, M 0.478; t50 neutral | population win | reviews |
| 7 | B1 on enterprise hosts | F&C | paper GHE ~2x, GitLab ~1.4x | population win | after #5353 |
| 8 | finish config-once (constant probes) | E2 car | +9 spawns since 703acd6f | W -0.2 s | low |
Projection after 1+3+4 (W) / 1+3 (L): W 11.5 -> ~8.7 s (1.16x over 7.5); L 5.9 -> ~5.4 s (1.55x over 3.5).
KILLED tonight: lazy imports (L 0.988, W 0.995), frozen/zipapp boot (no win; custom CPython), stat beyond form B (~1-2% W).
L GAP (-1.9 s still needed): integrate phase = 47% L (2.74 s): per-file Python passes (parse 0.26, hash re-reads, ledger record, journal 0.23, copy 0.17) + boot 0.45 (irreducible w/o fewer modules). Next research lane: single-pass integrate (each package file read once for hash+classify+parse+deploy) -- ceiling measurement next.
