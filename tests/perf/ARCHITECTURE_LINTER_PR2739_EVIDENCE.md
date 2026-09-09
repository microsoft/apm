# Architecture linter remediation evidence

This report records the PR #2739 NO-SHIP remediation measurements. It is
evidence, not a permanent performance promise.

## Revisions and environment

- Baseline: `cbe3bec00fe399381b84fa73562f12cc2159cac5` (`origin/main`)
- Benchmarked candidate: `12a661b210` (production linter unchanged from
  `a9be058453`)
- Host: macOS 26.6, arm64
- Runtime: CPython 3.14.3 for both linter entrypoints
- Tooling: uv 0.11.26
- Environment: `LC_ALL=C`, `PYTHONHASHSEED=0`,
  `UV_DEFAULT_INDEX=https://pypi.org/simple`
- Both worktrees were clean before measurement. Dependency setup and warmup
  were outside timing.

The timed command was identical in each worktree:

```console
/usr/bin/time -lp bash scripts/lint-architecture-boundaries.sh
```

`caffeinate -dimsu` wrapped the measurement driver. Each sample used a fresh
`/usr/bin/time` process. The order was three ABBA blocks, where A is baseline
and B is candidate.

| Seq | Timestamp (UTC) | Revision | Wall (s) | User (s) | Sys (s) | Peak RSS (MiB) |
|---:|:---|:---|---:|---:|---:|---:|
| 1 | 2026-08-31T19:58:24.769262Z | baseline | 49.93 | 38.39 | 3.25 | 187.83 |
| 2 | 2026-08-31T19:59:14.721270Z | candidate | 11.70 | 9.95 | 0.41 | 326.47 |
| 3 | 2026-08-31T19:59:26.441424Z | candidate | 11.76 | 9.96 | 0.41 | 329.67 |
| 4 | 2026-08-31T19:59:38.239256Z | baseline | 57.60 | 39.03 | 3.17 | 187.80 |
| 5 | 2026-08-31T20:00:35.849774Z | baseline | 55.10 | 38.63 | 3.39 | 187.75 |
| 6 | 2026-08-31T20:01:30.970936Z | candidate | 12.90 | 9.95 | 0.41 | 330.58 |
| 7 | 2026-08-31T20:01:43.903840Z | candidate | 12.05 | 10.02 | 0.41 | 330.69 |
| 8 | 2026-08-31T20:01:55.970320Z | baseline | 50.14 | 38.55 | 3.30 | 187.78 |
| 9 | 2026-08-31T20:02:46.129401Z | baseline | 50.34 | 39.78 | 3.06 | 187.78 |
| 10 | 2026-08-31T20:03:36.506038Z | candidate | 13.64 | 11.12 | 0.47 | 327.97 |
| 11 | 2026-08-31T20:03:50.161451Z | candidate | 13.26 | 11.27 | 0.47 | 331.98 |
| 12 | 2026-08-31T20:04:03.443246Z | baseline | 51.04 | 39.66 | 3.26 | 187.69 |

| Statistic | Baseline | Candidate | Change |
|:---|---:|---:|---:|
| Median wall | 50.69 s | 12.48 s | -75.4% |
| p90 wall (nearest rank) | 57.60 s | 13.64 s | -76.3% |
| Median user | 38.83 s | 9.99 s | -74.3% |
| Median sys | 3.26 s | 0.41 s | -87.4% |
| Median peak RSS | 187.78 MiB | 330.13 MiB | +75.8% |
| Maximum peak RSS | 187.83 MiB | 331.98 MiB | +76.7% |

The candidate remains above the legacy baseline's memory use. It is bounded
at about 332 MiB and is 67-69% below the independently observed 999-1,079 MiB
pre-remediation candidate. The reduction comes from compact array-backed tree
metadata and spilling cold `tests/` facts after their one parse, not from
weakening or skipping rules.

An earlier `/usr/bin/time` series was rejected because macOS slept between
samples: wall time increased by minutes while CPU time stayed flat. A
`resource.getrusage()` series was also rejected because macOS reports
`RUSAGE_CHILDREN.ru_maxrss` cumulatively within the driver. Neither rejected
series contributes to the statistics above.

## Deterministic work and full-catalog scaling

A clean candidate run reported:

- inventory files: 3,509
- files read: 1,813; maximum reads per file: 1
- Python files parsed: 858; maximum parses per file: 1
- AST visits: 1,500,469
- tree indexes built: 858; maximum builds per file: 1
- child processes: 0
- one inventory construction

The production-catalog scaling benchmark is reproducible with:

```console
PYTEST_PERF=1 \
APM_ARCH_SCALING_REPORT=/tmp/architecture-scaling.json \
uv run --frozen --extra dev pytest -q -m benchmark \
  tests/perf/test_architecture_linter_scaling.py::test_real_six_group_catalog_wall_time_scales_below_fifteen_x
```

It copied the real repository and added 0, 80, and 800 valid integration
modules. Each size ran three times. Fixed cost is the 0-file production-catalog
median.

| Added files | Wall samples (s) | Median (s) | Deterministic walk work |
|---:|:---|---:|---:|
| 0 | 11.518, 10.414, 11.009 | 11.009 | 18,039,813 |
| 80 (N) | 12.976, 13.109, 12.314 | 12.976 | 18,070,133 |
| 800 (10N) | 13.682, 16.018, 11.999 | 13.682 | 18,343,013 |

- Fixed-cost-adjusted 10N/N wall ratio: **1.36x**
- Fixed-cost-adjusted 10N/N deterministic-work ratio: **10.00x**
- Maximum reads and parses per file at every size: **1**
- Acceptance gate: **<15x**

CI runs the bounded 0/6/60 production-catalog variant explicitly in the
required `test-architecture` job.

## Comparable architecture test slice

Both revisions used the same CPython 3.12 candidate virtual environment, eight
xdist workers, and this command shape:

```console
files=$(find tests/unit/scripts tests/integration -maxdepth 1 \
  -name 'test_architecture_*.py' -print | sort)
python -m pytest -q -n 8 --dist worksteal $files \
  tests/unit/test_shepherd_owner_touch_gate.py \
  tests/unit/test_shepherd_driver_completion_schema.py
```

| Revision | Cases | Pytest duration | Wall | Result |
|:---|---:|---:|---:|:---|
| current main | 327 | 1,104.02 s | 1,111.01 s | 317 passed, 10 failed |
| candidate | 717 | 388.67 s | 394.58 s | 717 passed |

Seven baseline failures are the stale plugin fixtures identified in review.
Three are legacy Agent Plugin projection assertions. The candidate fixture
constructor writes the current minimal plugin `apm.yml`; no product behavior
was changed. Despite running 390 more cases, candidate wall time is 64.5%
lower.

## Functional and mutation parity

The executable normalized matrices are:

- `tests/integration/test_architecture_owner_rule_mutations.py`
- `tests/integration/test_architecture_semantic_rule_mutations.py`
- `tests/perf/architecture_linter_parity_matrix.json` (independent
  current-main and candidate outcomes for every case)

They assert set equality against every registry guard and every guard-less
registered rule, use surgical in-memory source overrides, reject syntax/read
failures as proof, and require each selected rule to blame itself.

Results:

- clean current-main legacy entrypoint: exit 0 in all six ABBA samples
- clean candidate entrypoint: exit 0 in all six ABBA samples
- candidate mutations: 103/103 detected, zero runner failures
- the same 103 mutations each ran independently against a restored clean
  current-main copy through the retired shell entrypoint: 100/103 detected
- the three legacy misses are `install-deployment-base-integrator`,
  `install-deployment-outcome`, and
  `registry_delegation.manifest_schema_negotiation`; the candidate detects all
  three, so the semantic catalog strengthens rather than weakens those seams

The legacy runs used eight isolated worktree and virtual-environment copies.
Every row records the exact mutation intent, baseline exit code, wall time,
observed historical aliases, output hash, candidate semantic outcome, and
revisions. Historical AC numbers were duplicated, so the raw aliases remain
display metadata; candidate attribution is exact at semantic-rule granularity.

## Conflict and fail-closed proofs

`tests/unit/scripts/test_architecture_registry_conflicts.py` proves two
independent owner additions to different domain shards merge cleanly and do
not modify:

- `.apm/instructions/architecture.instructions.md`
- `.github/instructions/architecture.instructions.md`
- the instruction deployment hash in `apm.lock.yaml`

Registry contract tests also reject malformed, missing, and unlisted shards.
The retired checker inventory contract prevents the 25 deleted executable
authorities, or imports of them from the canonical linter, from returning.
