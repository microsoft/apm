# Perf fixtures and journey benchmarks

Durable, seeded fixtures for measuring the real `apm` CLI on realistic
workloads. Tests and benchmarks reuse them; nothing here is a binary blob.
The generator, the pinned recipes and the counters are the asset.

## Fixtures

| Recipe | Shape |
|---|---|
| `tree-10`, `tree-50`, `tree-200` | A layered dependency DAG with 5, 6 and 8 levels. It has transitive overlap (level-skipping edges) and diamonds (two or more parents). Every package ships 4 instructions, 2 agents, 2 skills (each with a reference file) and 3 prompts. Every 3rd package adds a hook and every 8th a stdio MCP server. |
| `monorepo-4` | One repository with 5 `apm.yml` roots (the repo root plus `apps/app-00..03`). The roots share 12 local packages by relative path plus a 20-package remote tree. |
| `real` | Curated real public packages on github.com (`REAL_PACKAGES` in `real_network.py`), pinned to tags or SHAs. |

The same recipe always produces byte-identical trees and identical git SHAs
(fixed commit identity and date). To change the workload, add a new recipe in
`recipes.py` rather than editing a pinned one.

```python
from tests.fixtures.perf import TREE_RECIPES, build_tree, serve_offline

tree = build_tree(TREE_RECIPES["tree-50"], tmp_path / "fixture")
with serve_offline(tree, tmp_path / "world", rtt_ms=30) as world:
    subprocess.run([apm, "install"], cwd=tree.project, env=world.env, check=True)
    print(world.traffic().as_dict())  # connects, requests, round_trips
```

`serve_offline` serves the packages under GitHub-shaped `owner/repo` slugs
through the lifecycle world's `WorldServices`: a recording HTTPS proxy in
front of `git http-backend`. It gives the child a private HOME, APM_HOME and
cache (`IsolatedApmEnvironment`). `world.clear_cache()` makes the next command
cold. The proxy adds `rtt_ms` per round trip: 2 for each new HTTPS
connection (TCP + TLS) and 1 for each request.

## Journey harness

`tests/benchmarks/journeys.py` runs the CLI as a subprocess. It finds the CLI
from `--apm`, then `APM_BINARY_PATH`, then `.venv/bin/apm`. The journeys are:

- boot: `--version`, `--help`
- install: cold and warm cache, no-op, `--frozen` CI replay (committed
  lockfile, cold cache), locked warm (`install-locked-warm`) and `--frozen`
  warm (`install-frozen-warm`): a committed lockfile, no `apm_modules`, warm
  cache; `-g` cold and warm
- `update -y`, `uninstall`
- local: `compile`, `audit`, `pack`, and `install-bundle` (the supported
  unpack path: a fresh project runs `apm install <bundle>`; `apm unpack` refuses
  bundles that carry hooks)

It writes `journeys-<sha>.json` and `.md`. Each cell reports wall p50 and p90,
CPU p50 (user + sys of the `apm` child and every descendant it waited for, so
the mirror's server-side work is excluded), request, round-trip and connect
counts, and the 1-min load of every sample. It also adds a scaling table for
the tree sizes. `--counts` adds subprocess spawns per executable and the
number of imported modules, through an audit-hook `sitecustomize` on the
child's `PYTHONPATH` (`tests/benchmarks/spawn_counter/`).

The North Star scoreboard is one preset: tree-10 at 30 ms RTT, 5 runs, with
counts. Its modes are no-op, warm (`install-locked-warm`), fresh
(`install-warm`), frozen (`install-frozen-warm`), empty cache
(`install-cold`, network-bound) and boot (`--version`). `--scoreboard FILE`
appends one row per run with wall and CPU p50 per mode and the gap to 1 s.
Explicit flags override the preset, for example `--runs 1 --profile`.

```bash
# Smoke: one run, no RTT
uv run python -m tests.benchmarks.journeys run --fixtures tree-10 --runs 1 --rtt 0

# North Star scoreboard (tree-10, rtt 30, 5 runs, CPU and counts)
uv run python -m tests.benchmarks.journeys run --preset scoreboard --scoreboard scoreboard.md

# Baseline: scaling curve x RTT profiles, cProfile per cell
uv run python -m tests.benchmarks.journeys run \
  --fixtures tree-10,tree-50,tree-200,monorepo-4 --rtt 0,30,100 --runs 5 \
  --profile --out build/perf

# Real network: re-check that every curated package still resolves, then measure
uv run python -m tests.benchmarks.journeys verify-real
uv run python -m tests.benchmarks.journeys run --fixtures real --runs 3
```

A sample counts only when the 1-min load is under `--max-load` (default 30).
Before each timed run, the harness waits up to `--load-wait-min` minutes for
the load to drop. All journeys run at RTT 0. Only the `--rtt-journeys` (default: install cold,
warm and frozen, and update) repeat at the other RTTs. Requests are counted
everywhere, so any network call from a local command shows up. On the
monorepo, `-g` and pack journeys are skipped, because pack refuses local-path
dependencies by design.

In real-network mode, a counting CONNECT tunnel reports connects and bytes.
Request counts need TLS termination, which only the offline mode has. Set
`GITHUB_APM_PAT` to avoid anonymous API rate limits.

## Opt-in tests

```bash
PYTEST_PERF=1 uv run pytest tests/perf/test_perf_fixtures.py -m benchmark -s
```

Default CI never runs them, because `tests/perf` needs `PYTEST_PERF=1` and
`benchmark` is deselected by default.
