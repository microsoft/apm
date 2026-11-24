# Boot floor and the import lever (PERF-SOL, 2026-11-24, CI only)

HEADLINE: boot = interpreter 0.01 L / 0.02 W + install-path imports 0.39-0.47 L / 0.45-0.55 W
+ click plumbing 0.01 = ~0.45 s L / ~0.55 s W. Imports are now ~8.5% of L cold t50 and ~5% of W
(B2's 2.6% is stale: imports stayed flat while cold shrank 2.5x). Lazy-import prototype: L 0.988,
W 0.995 paired t50 -> KILL. Frozen/zip forms: no win; frozen apm_cli not reachable for pip/uv installs.
Only real boot money: missing pyc (+0.5-0.7 s/run if site-packages is read-only; +0.7-0.9 s once after uv install).

Runs: probe on main c0296fab = 45452267627 (branch perf-research/arch-boot 0b175481;
scripts/perf/boot_probe.py + importtime_report.py; 21 runs per row, CPU incl. children via
the bench's own _run_measured = wait4 / Job Object). Runners ubuntu-24.04 / windows-2025, py 3.12,
CLI installed exactly as the bench does (uv sync --frozen --no-editable, venv console script).
pop: anon (boot is population-independent). W CPU timer granularity 15.6 ms.

## (a) Boot split, CPU ms p50 (L / W)

| row | L | W |
|---|---|---|
| python -S -c pass (interpreter, no site) | 6.8 | 15.6 |
| python -c pass (+site) | 9.3 | 15.6 |
| python -X frozen_modules=off -c pass | 10.8 | 15.6 |
| import click | 31.9 | 46.9 |
| apm --version (console script; W incl. launcher) | 93.8 | 125.0 |
| apm --help | 134.3 | 203.1 |
| import apm_cli.cli + apm_cli.commands.install | 396.2 | 468.8 |
| apm install --help (install verb loaded, no work) | 408.5 | 484.4 |
| IN-SITU install-cold t50 imports (importtime, clamped self) | 415 | 500-566 |
| install-cold t50 total CPU, same runner, rtt 0, no counters (3 runs) | 4371 | 8750 |

In-situ import tree (install-cold t50, 2 traces/OS): 917 modules (510 apm_cli).
Self time L: apm_cli 289-295 ms, stdlib 61, third-party 62 (rich 15, urllib3 14, tomlkit 9,
yaml 7, filelock 4.5, requests 3.4, click 3.1). W: apm_cli 338-365, stdlib 90, third-party 93
(rich 34, urllib3 15, yaml 9, click 7, tomlkit 6). Caveat: importtime's nesting counter is
global, so imports done on fetch worker threads (cache.inprocess_fetch, deps.anonymous_ref_listing)
corrupt per-module self on W (2 negative rows); totals use self clamped at 0.

Top modules by self (L, ms): compilation.agents_compiler 14.4, urllib3.util.url 6.9,
policy.schema 6.7, agent_plugins.ir 6.2, marketplace.yml_schema 5.0, tomlkit._utils 4.7,
marketplace.builder 4.1, core.deployment_state 4.1, install.context 3.9, utils.git_env 3.4,
filelock._api 3.3, security.trust 3.1, yaml.reader 3.0, security.executables 2.8, deps.lockfile 2.7.
No single module dominates: apm_cli is ~0.57 ms x 510 modules of module-body execution.

Top by cumulative (L, ms): deps.registry 92.7 -> registry.resolver 87.3 -> bundle.local_bundle 86.4
-> bundle.packer 83.5 -> bundle.lockfile_enrichment 82.6 -> integration 82.4 (needed anyway);
commands 69.2 -> commands.deps 69.1 -> commands.deps.cli 67.3 (OFF-PATH: reached via a package
re-export); entrypoint 62.9 / version 62.0 (the apm_cli package boot); compilation 29.6 ->
agents_compiler 28.9 (OFF-PATH: via compilation.link_resolver -> package __init__ re-export);
marketplace 22.4 -> builder 16.5 (OFF-PATH: via marketplace.version_pins -> package __init__).

### Split of the profile's "rest/imports/cli" bucket (L 1.08 s, W 2.72 s; v039 profiles)

| sub-bucket | L s | W s | source |
|---|---|---|---|
| interpreter start | 0.009 | 0.016-0.019 | python -c pass (W: unit-costs 45359600677 = 18.8 ms) |
| imports (install path, in-situ) | 0.41 | 0.50-0.57 | importtime above |
| CLI plumbing (click parse + dispatch + lazy verb load) | 0.012 | 0.016 | apm install --help minus the import row |
| NOT boot: remainder of that profile bucket | ~0.65 | ~2.1 | runtime work the profile lumps into "rest" |

So boot = ~0.43 s L / ~0.52 s W. The rest of the bucket is not interpreter/imports/click.

### Does B2's 2.6% still hold? No, not on Linux.
Imports are flat (~0.41 L / ~0.53 W) while cold shrank:
- L: 0.41 / 4.37 = 9.4% (probe world) or 0.41 / 5.76-5.97 = 7% (bench world).
- W: 0.53 / 8.75 = 6% (probe world) or 0.53 / 16.7-19.1 = 2.8-3.2% (bench world).

## (b)(ii) Compiled / frozen boot

| form | L ms | W ms | verdict |
|---|---|---|---|
| pyc in __pycache__ (site-packages, steady state) | 396 | 469 | today |
| apm_cli from source dir with pyc | 397 | 484 | = site-packages (no path-cost) |
| apm_cli from a zip of pyc only (zipimport) | 386 | 500 | +-3%, noise: no win |
| apm_cli without pyc (-B, recompiles every run) | 903 | 1156 | +0.51 / +0.69 s per run |
| FIRST run after uv sync (writes pyc) | 1106 | 1406 | one-time +0.71 / +0.94 s |

- Precompiled .pyc: uv does not compile bytecode by default (pip does), so the first `apm` after
  a uv install pays +0.7 s L / +0.9 s W once. A user whose site-packages is read-only or who sets
  PYTHONDONTWRITEBYTECODE pays +0.5 / +0.7 s on EVERY run. Cheap fix on paper: doc
  `uv tool install --compile-bytecode` (or UV_COMPILE_BYTECODE=1); not a code lever.
- zipapp vs site-packages: measured no import-time win (386 vs 396 L; 500 vs 469 W).
- Frozen modules (3.11+): -X frozen_modules=off costs +1.5 ms L. Frozen modules are only the
  stdlib startup set baked into the interpreter at CPython build time; freezing apm_cli needs a
  custom CPython build (Tools/freeze / deepfreeze). NOT reachable for a pip/uv-installed CLI.
  The release binary is already the archive form (PyInstaller onedir, optimize=2, build/apm.spec:291,346),
  and the zip row says that form buys nothing over pyc.
- Ceiling of (ii): 0 s steady state; the only real money is the pyc-missing tail above.

## (b)(i) Lazy-import prototype (perf-research/arch-lazy-imports, product SHA cf8c2d96)

- Clicks verbs are ALREADY lazy on main (cli.py:445 stubs, cli.py:580-587 _LazyCommand), so the
  only lazy room is inside the install path. The importtime tree shows the off-path subtrees come
  in through eager package re-exports: compilation/__init__ (via compilation.link_resolver),
  marketplace/__init__ (via marketplace.version_pins), commands/deps/__init__ (via commands.deps).
- Prototype: PEP 562 module __getattr__ in those 3 inits (all `from .x import y` -> lazy map;
  `from pkg import Name` keeps working). Tool: files/tools/lazy_init.py.
- Deterministic effect (importtime, probe 45454473172 vs 45452267627): 917 -> 890 modules;
  25 modules avoided on both OS (11 compilation.*, 13 marketplace.*, commands.deps.cli), worth
  30.2 ms L / 24.4 ms W of import self-time = 0.5-0.7% of L cold, 0.2-0.3% of W cold.
- Most of the 110 ms "cumulative" of those subtrees is modules install needs anyway later, so
  deferring the re-export recovers only the 25 truly unused modules.
- Paired 6-pair bench install-cold vs main c0296fab (rtt 30, pop: anon), 0 errors, requests/round trips identical:
  | OS | t10 paired | t50 paired | t50 base -> new CPU s | run |
  |---|---|---|---|---|
  | L | 0.982 | 0.988 | 8.623 -> 8.459 | 45454557177 |
  | W | 0.998 | 0.995 | 18.375 -> 18.555 | 45454614998 |
- Verdict: KILL as a perf lever (<= 1-2% L, noise on W). The import bucket is ~0.4-0.5 s spread over
  ~500 apm_cli module bodies (~0.6-0.9 ms each); no off-path subtree worth more than 30 ms remains.
  Only an install path that imports fewer of its OWN modules (design, not laziness) can move it.

### Runner variance (why only paired / deterministic numbers count)
Probe main vs probe lazy ran on different runners: `python -S -c pass` 6.8 -> 8.5 ms and
`import click` 31.9 -> 39.3 ms (+23%), which the change cannot touch; install-cold t50
4.37 -> 6.08 s L and 8.75 -> 12.39 s W. Cross-job absolute CPU moves 25-40% with the runner.

## Replicate + A/A world check, same runner (run 45454873425, arch-boot a3378e31, main product)

Boot rows (CPU ms, L / W): python -S 8.0 / 15.6; python -c pass 10.8 / 15.6; import click 36.7 / 62.5;
apm --version 116 / 172; import cli+install 482 / 563; apm install --help 490 / 594.

install-cold t50, 3 runs each, SAME runner:
| world | L CPU s | W CPU s |
|---|---|---|
| rtt 0, counts off | 5.67 | 12.16 |
| rtt 0, counts on | 5.71 | 12.42 |
| rtt 30, counts off | 5.81 | 12.16 |
| rtt 30, counts on (= bench world) | 5.79 | 12.30 |
=> rtt and --counts move CPU <= 3%. The probe-vs-bench absolute gaps are RUNNER variance:
main L install-cold t50 today reads 4.37 (45452267627), 5.59-5.81 (45454873425), 8.62 (bench
45454557177 base side), 5.97 (v039 45446611653). Absolute targets (L <= 3.5 s, W <= 7.5 s) can
only be judged with a runner-normalized yardstick (e.g. report CPU / `python -c "import click"`
on the same job), never from one job's absolute.

### Imports as a share of cold t50, same-runner (clean row: import cli+install minus interpreter)
| run | L imports s | L cold s | L % | W imports s | W cold s | W % |
|---|---|---|---|---|---|---|
| 45452267627 | 0.387 | 4.37 | 8.9% | 0.453 | 8.75 | 5.2% |
| 45454873425 | 0.471 | 5.67-5.81 | 8.2% | 0.547 | 12.16-12.42 | 4.5% |
B2's 2.6% no longer holds: imports are now ~8.5% of L cold and ~5% of W cold (they stayed flat while cold shrank).

### Open flag: bench absolute vs direct journeys run (same SHA, same harness code)
bench base side (ci_bench abab, 6 pairs) t50: L 8.62 / W 18.38 s; direct `journeys run` on another job:
L 5.67-5.81 / W 12.16-12.42 s. rtt and counts are ruled out (A/A above). Untested candidates:
the bench interleaves two CLIs on one runner (shared cache/AV state), or a runner class difference.
Paired ratios stay valid; absolute-vs-target claims should wait for one same-job calibration.
