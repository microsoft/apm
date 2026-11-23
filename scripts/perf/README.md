# Perf bench on CI runners

`bench_ci.sh request BASE NEW [JOURNEYS] [FIXTURES] [PAIRS] [RTT] [OS] [PROFILE]`
pushes a bench request; the Perf bench workflow runs base and new builds in an
offline world (`tests/benchmarks/journeys.py`, `tests/fixtures/perf`) and
reports CPU (user + sys, including waited children), wall at a modeled RTT,
and request, round-trip, connect and spawn counts. `bench_ci.sh fetch BRANCH`
prints the summary.

## Verdict method for an overhead bar

Use this method whenever a PR claims a performance bar (for example "full-path
overhead <= 1%").

1. **Pre-declare one run per head.** Before it starts, name the run that is the
   verdict for the PR's final head. No rerun until a pass. If a run fails for a
   runner error, report both runs and the reason in the PR body.
2. **15 interleaved pairs.** `PAIRS=5` on 3 runners: 15 base/new pairs,
   interleaved. At 9 pairs the run-to-run noise of the paired ratio is about
   +-2%, larger than a 1% bar.
3. **One statistic, every cell, one run.** Per cell (journey x fixture x OS),
   the paired new/base CPU ratio p50 with its 95% band. Report base and new CPU
   p50 in seconds beside it. No dropped pairs or outliers; every cell of the bar
   comes from the same run.
4. **Pass, fail or inconclusive.** A cell passes at paired p50 <= 1.010. If any
   cell's 95% upper bound is above 1.020 the run is INCONCLUSIVE, not a pass:
   rerun once with 30 pairs (`PAIRS=10`), and that result is final.
5. **Pre-register.** This method is merged (or at the merge desk) before a PR
   claims a pass with it.
