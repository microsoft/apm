# Bench calibration: bench path vs direct path, same VM (PERF-SOL, run 45457579415)

Workflow perf-research/arch-calib 7b877c17 (perf-sol-calib.yml), 4 VMs per OS, main c0296fab installed exactly as perf-bench.yml.
Per VM: CPU model, yardstick (scripts/perf/yardstick.py: process CPU of a fixed pure-Python loop, sha256 256 MB, 2000 small files),
direct `journeys run` install-cold t50 (3 runs, rtt 30, --counts) then `ci_bench abab` A/A (2 pairs, base = new = same CLI), pop: anon.

| VM | CPU | py_loop s | fs s | direct t50 CPU s | bench path (4 samples) | bench/direct | t50 / py_loop |
|---|---|---|---|---|---|---|---|
| L1 | EPYC 7763 | 0.622 | 0.166 | 8.26 | 8.28-8.44 | 1.010 | 13.3 |
| L4 | EPYC 7763 | 0.612 | 0.167 | 7.99 | 8.07-8.22 | 1.026 | 13.1 |
| L2 | EPYC 9V74 | 0.513 | 0.123 | 6.08 | 6.09-6.29 | 1.034 | 11.8 |
| L3 | EPYC 9V74 | 0.700 | 0.158 | 7.61 | 7.62-8.08 | 1.036 | 10.9 |
| W2 | EPYC 7763 | 0.656 | 1.422 | 17.67 | 17.78-18.27 | 1.014 | 26.9 |
| W3 | EPYC 7763 | 0.656 | 1.453 | 17.19 | 17.70-19.00 | 1.040 | 26.2 |
| W1 | EPYC 9V74 | 0.516 | 1.031 | 13.17 | 12.45-12.89 | 0.958 | 25.5 |
| W4 | EPYC 9V45 | 0.359 | 0.656 | 8.45 | 7.98-8.50 | 0.977 | 23.5 |

Cause: hosted-runner CPU class, not the bench path. Same-VM bench/direct = 0.96-1.04.
- W install-cold t50 on main spans 8.45 s (9V45) -> 13.2 s (9V74) -> 17.2-17.7 s (7763): 2.1x between classes.
  Windows file syscalls swing even more (fs yardstick 0.66 -> 1.45 s).
- L spans 6.1 s (9V74) -> 8.0-8.3 s (7763): 1.36x.
- pyc RULED OUT: 0 pyc after uv sync, 19 after `apm --version`, 497 apm_cli pyc after the first measured run.
  Only the first sample per side per shard pays compile (+0.5-0.7 s), and the 6-sample median absorbs it.
- The bench's 3 shards land on different VMs. Per-shard samples are tight (+-1%) but sit at different levels:
  45454557177 L shards 8.7 / 5.9 / 8.6 s; 45446611653 L shards 8.7 / 6.0 / 4.8 s.
  The pooled "base CPU p50" is a class mix, so it is meaningless as an absolute. Paired ratios stay valid,
  because base and new alternate on the same VM.
- Normalized CPU (t50 / py_loop) is stable within +-10% across classes: L ~11-13, W ~24-27 yardstick units.

Absolute numbers to hold against the 5x targets (L <= 3.5 s, W <= 7.5 s), by class, main c0296fab:
| class | L main | L stack (x0.972 anon) | W main | W stack (x0.875 anon) |
|---|---|---|---|---|
| EPYC 9V45 (fastest seen) | n/a (not drawn on L) | n/a | 8.45 | ~7.4 (meets 7.5) |
| EPYC 9V74 | 6.1 | ~5.9 | 13.2 | ~11.5 |
| EPYC 7763 | 8.0-8.3 | ~7.9 | 17.2-17.7 | ~15.3 |
Recommendation: state targets in one class or in yardstick units (e.g. W <= 7.5 s on 9V74 is about 14.5 py_loop units).
The harness should record the CPU model per sample and report per-class (or per-shard) absolutes.
