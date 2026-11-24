# v0.39.0 PROJECTED STACK table (PERF-SOL, 18:4x CET)

## Stack contents
`perf-research/arch-v039-stack` e62f520f = main 1daf3015 plus the heads below.
- Already on main: #5330, #5341, #5351, #5361 (emit-once), #5325, #5318, #5132, #5283.
- Merged clean on top:
  - #5353 authenticated in-process fetch (48913f6f)
  - #5354 resolve roots once (b0cd35fb)
  - #5366 pack checkout, which supersedes the closed #5352 (5351de6c)
  - #5346 resolve-count (fe749b26)
  - Windows-paths engineer head (danielmeppiel-perf-engineer-windows-paths d64537ed)
  - lever S (perf-research/arch-lever-s 6bcee9af)
- NOT in: #5367 small-cars train (1ea1c3fe). It has a semantic conflict in deps/lockfile.py with #5361 emit-once (both rewrote LockFile.write's read/emit path). Needs a rebase by its owner. No warm-train head was found besides #5367.

## Method
- Paired CPU incl. children, p50 of 6 interleaved pairs, base = main 1daf3015.
- Journeys `install-cold` (anon, public github.com) and `install-cold-auth` (auth, token-first), same run per OS.
- Windows in two worlds:
  - (a) today's bench world (OpenSSL backend + CA file): default harness.
  - (b) the real Git-for-Windows default (system http.sslBackend=schannel copied into the isolated global): BETS' 722a8662 rebased onto main as perf-research/arch-wintls-main.
- Absolutes come from different runner draws per run; trust within-run ratios. One profiled sample per cell for buckets.

## Table (t50; t10 in brackets)
| OS / world | pop | main CPU s | stack CPU s | paired | 5x target | stack vs target | floor gap at stack | run |
|---|---|---|---|---|---|---|---|---|
| Linux | anon | 5.97 | 5.76 | 0.972 [0.995] | 3.5 s | 1.65x over | 8.2x (0.7 s) | 45446611653 |
| Linux | auth | 10.82 | 5.82 | 0.535 [0.630] | 3.5 s | 1.66x over | 8.3x | 45446611653 |
| W (a) bench TLS | anon | 19.09 | 16.73 | 0.875 [0.955] | 7.5 s | 2.23x over | 11.2x (1.5 s) | 45446661279 |
| W (a) bench TLS | auth | 38.78 | 16.90 | 0.426 [0.505] | 7.5 s | 2.25x over | 11.3x | 45446661279 |
| W (b) schannel (real default) | anon | 13.23 | 12.52 | 0.929 [0.974] | 7.5 s | 1.67x over | 8.3x | 45446730214 |
| W (b) schannel (real default) | auth | 31.27 | 12.98 | 0.428 [0.502] | 7.5 s | 1.73x over | 8.7x | 45446730214 |
- Requests at t50: anon 101 -> 101, auth 250 -> 100. Git spawns at t50: anon 18, auth 365 -> 20.
- The world (a) vs (b) absolutes differ by ~4 s on both base and stack. That is a different runner draw, possibly plus OpenSSL CA-file loading per connection; not attributable from these runs. Run one paired a-vs-b A/A to size the TLS world itself.

## Top 3 remaining buckets at the stack (one profiled sample per cell, t50)
| cell | #1 | #2 | #3 | run |
|---|---|---|---|---|
| L anon (5.88 s) | path resolution + containment 1.60 s (27%; _kernel_realpath 1.18) | rest/imports/cli 1.08 s (18%; imports 0.51) | primitive parse/integrate 0.67 s (11%) | 45446802295 |
| L auth (5.97 s) | path resolution + containment 1.59 s (27%) | rest/imports/cli 1.21 s (20%) | file writes/copies 0.68 s (11%) | 45446802295 |
| W (a) anon (16.55 s) | path resolution + containment 4.86 s (29%; _form_b 1.87, _entry 0.72, portable_relpaths 0.65, _form_b_missing 0.62) | rest/imports/cli 2.72 s (16%) | file writes/copies 2.18 s (13%; in-process fetch _write_file 0.50) | 45446854372 |
| W (a) auth (16.41 s) | path resolution + containment 4.85 s (30%) | rest 2.57 s (16%) | file writes/copies 2.27 s (14%) | 45446854372 |
| W (b) anon (12.00 s) | path resolution + containment 3.37 s (28%) | rest 1.96 s (16%) | file writes/copies 1.47 s (12%) | 45446906357 |
| W (b) auth (12.00 s) | path resolution + containment 3.63 s (30%) | rest 1.70 s (14%) | file writes/copies 1.62 s (14%) | 45446906357 |
- Leaf kinds W (a) anon: stat 3.25 s, file I/O 2.65, realpath 1.74, walk 1.02, imports 0.72.
- Git children are <= 0.34 s in every cell.

## Readout
- With the full queue, auth cold t50 drops ~2.3x on every OS. Anonymous moves only 3-13%, because B1/#5351 are already on main.
- No cell reaches the 5x target: L is 1.65x over 3.5 s; W is 1.7x (real default) to 2.2x (bench world) over 7.5 s.
- The largest remaining bucket everywhere is still path resolution + containment: ~27% L, ~30% W, even with the Windows-paths engineer's head in.
