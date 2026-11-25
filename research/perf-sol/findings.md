# PERF-SOL findings
## F1 (10:4x CET) B3 remainder beyond materialize-from-memory: KILLED (no measured win)
- Branch perf-research/arch-tree-deploy-sol on b69efdf0. W3d-conformant form = pin-before-seal (P1 marker written by the payload writer before seal, P2 differing marker drops seed, P4 seal-time scandir names + lstat==fstat). Differential SHA d9724d03 (walk vs seeded listing, raises on mismatch): L t10/t50 pass, 0 errors (45365177042). Measured SHA fe1c701a vs b69efdf0, 6 pairs: L t50 cold 11.95 -> 11.99 s paired 1.011 (45365230317), t10 0.998; macOS t50 1.018, t10 0.974 (45365251729). Counts unchanged (101 req, ~240 spawns).
- Hit evidence (profile 45365280552): seeded content hash now engages (package-tree reads 0.087 -> 0.015 s), inventory walk replaced by seeded subtrees; but the faithful seal (P4 scandir+lstat per file) and the substituting walk cost about what they save; discovery's per-file stat path stays (base is the project root).
- Re-seal variant 9d3d3b50 (L 1.005, M 0.983; 45363350066 / 45363392644): W3d NO, not a claim.
- Root cause on record: cache_pin writes .apm-pin into every package root after materialize, voiding b69efdf0's seed proof; b69's L 0.932 (45358796767) therefore comes from materialize-from-memory (no copytree, no re-read verify), not from hash seeding.
- B3 cumulative vs b0f7fde6 = b69efdf0 alone: L 0.932 (12.16 -> 11.43 s). macOS for b69 not yet measured (45358987432 was Linux deep attribution).

## F2 (11:1x CET) Rank 5 git-config probes: size and overlap with B1 (#5330) / #5318, cold t50
Harness perf-research/arch-sol-attr (git config variant + innermost apm caller per spawn), PROFILE=1, 1 profiled run per cell; seconds are on that run's runner.
| SHA | OS | total s | config spawns: --show-scope --list / --get-urlmatch / --get-regexp | config child CPU | git remote (git_clone_env) | parent git_env CPU | rank-5 family | run |
|---|---|---|---|---|---|---|---|---|
| main d849de7b | L | 20.10 | 104 / 108 / 7 | 0.34 s | 50: 0.11 s | 0.67 s | ~1.1 s (5.6%) | 45368202055 |
| main d849de7b | W | 46.14 | 104 / 108 / 8 | 4.42 s | 50: 0.88 s | 1.80 s | ~7.1 s (15.4%) | 45368262161 |
| #5330 B1 340a3f9f | L | 9.50 | 4 / 8 / 8 | 0.02 s | 0 | 0.05 s | ~0.03 s (B1 deletes ~95%) | 45368304644 |
| #5330 B1 340a3f9f | W | 49.03 | 104 / 108 / 8 | 4.52 s | 50: 0.89 s | 2.05 s | ~7.5 s (B1 NOT engaged on Windows at this head) | 45368360184 |
| #5318 f4c99b8b | L | 21.89 | 122 / 108 / 7 | 0.34 s | 50: 0.10 s | 0.61 s | ~1.0 s | 45368401234 |
| #5318 f4c99b8b | W | 50.52 | 122 / 108 / 8 | 5.13 s | 50: 0.94 s | 1.67 s | ~7.7 s (#5318 adds 18 show-scope spawns) | 45368473455 |
Callers: every config spawn comes from git_env._run_git_config (clone-env setup per package, ~2 --show-scope --list + ~2 --get-urlmatch per package); git remote x50 from git_env.git_clone_env.
Non-overlap: (1) Windows until B1 engages there: ~7.1 s = 15% of W cold t50; (2) every non-github.com host on every OS (B1 is github.com-only by W3d F10): ~1.1 s L / ~7.1 s W at t50; (3) after B1 engages, residual ~20 config + 2 var + 2 remote spawns = ~0.03 s L / ~0.5 s W (est. 24 x ~21 ms).
Other form (same guarantee: user git config honored per remote): read the effective config ONCE per command (one `git config --list --show-scope --null`) and evaluate url.<base>.* / http.<url>.* urlmatch in process; derive the remote URL instead of spawning `git remote`. Cost 1 spawn = ~1 ms L / ~21 ms W + ~2 ms parse -> saves ~1.1 s L / ~7 s W on paths B1 does not cover. #5318 regresses show-scope spawns 104 -> 122 (+18 x ~27 ms = ~0.5 s W).

## F3 (11:4x CET) Rank 5 git config read once: W t50 cold 0.842, L 0.929 (B1 off)
f47c62c5 vs main d849de7b, 6 pairs: W t50 44.64 -> 37.59 s (0.842; 45371171795), t10 0.837; L t50 17.77 -> 16.84 s (0.929; 45371142590), t10 0.960. Spawns t50 674 -> 365 (config show-scope 104 -> 4, get-urlmatch 108 -> 0, remote 52 -> 2, probe init 50 -> 0). Differential SHA 3594c07b 0 errors L/W (45371071882 / 45371110863); BETS cells 23/23 L/W. W3d D1-D8: D1/D4/D7 folded at 28f4e939 (re-bench pending). Handoff files/rank5-handoff.md.

## F4 (12:0x CET) Git-path levers 2b + 3, population: public github.com anonymous, B1 off
7b056a1b vs 28f4e939, 6 pairs: W t50 27.91 -> 26.45 s (0.933; 45375572721), t10 0.950; L t50 0.975 (45375535638), t10 0.989. ls-tree 100 -> 50 (lever 3), cat-file -t 50 -> 0 (lever 2b), spawns 364 -> 266 (profile 45375634366). Differential SHA 1231c697 0 errors L/W (45375475046 / 45375501015). W3d: 2b OK-with V1-V6 (V1 idx errors -> None, V2 replace refs -> None required), 3 OK-with L1-L5 (L1 invocation_memo + copies, L2 replace refs / object format required). 2a and 4 need own rulings; 5 skipped (~0.1 s W).

## F5 (12:2x CET) Authenticated population (install-cold-auth, JH harness 3ca6002e, B1 off), 6 pairs
Baseline main d849de7b t50: L 14.66 s, W 42.82 s; 815 spawns (config 312, clone 100, ls-tree 100, cat-file 100, remote 52, init 50, ls-remote 50, checkout 50).
Config-once 28f4e939 vs main: W t50 0.826 (-7.7 s; 45377500317), t10 0.855; L t50 0.918 (45377398798). Spawns 815 -> 415.
Git-path 2b+3 7b056a1b vs 28f4e939: W t50 0.922 (-2.2 s; 45377540762), L 0.972 (45377460191). ls-tree/cat-file 100 -> 50 each.
Stacked auth: W ~0.76, L ~0.89.

## F6 (13:1x CET) Lever 4 (in-process checkout .git instead of clone --shared): KILLED (kill rule: W auth t50 not < 0.97)
Fix head 518a7673 (forward-slash alternates, byte-exact differential, loud failure on in-process checkout error) vs 7b056a1b, install-cold-auth, 6 pairs: W t50 1.178 (22.56 -> 27.27 s; 45382610173), t10 1.156; L t50 0.944 (14.15 -> 12.99 s; 45382652136), t10 0.980. Differential SHA 3b7d517c on W: config entries + alternates byte-equal to a real clone --shared, checkout succeeded, install OK (45382565926) -- yet git verbs show a full network re-fetch per package on Windows (fetch +50, init +50, round trips 453 -> 653, spawns 315 -> 415): a later reader (trust/verify of the landed checkout) rejects the in-process .git on Windows only and the package is re-materialized. Unidentified within the 30-min box; also ~165 src lines (> ~120). Linux-only gain 0.944 noted; re-open only with the Windows rejection root-caused (candidate: a .git file the trust walk expects that clone writes on Windows, e.g. packed-refs/refs/remotes, or a Windows-only reader).
Earlier unfixed head a976850d: W 1.173 (45379914691), L 0.969 (45379854769).

## F7 (13:3x CET) N1 attribution on the post-landing stack f251f785 (B1 engaged on L+W), install-cold t50, clean sol-attr (for BETS files/n1.md)
L (45384273602, sampled 9.12 s): containment+portable_relpath 2.04 (22.4%), YAML 1.82 (20.0%), discovery/re-walks 1.61 (17.7%), copies/writes 0.45, journal/ledger 0.37, re-hash/verify 0.24, rest 2.58 (imports 0.56).
W (45384299727, sampled 10.05 s): containment 1.91 (19.0%), discovery 1.64 (16.3%), YAML 1.27 (12.6%), copies/writes 1.06 (10.6%), journal/ledger 0.62, re-hash 0.23, rest 3.31.
Children ~0 (L 0.03 s, W 0.30 s). Top 3 remaining buckets after B1: containment/realpath, YAML, discovery re-walks (~5.5 s L / ~4.8 s W of ~9-10 s).

## F8 (14:1x CET) NATIVE-CORE GATE (perf-native-decision step 8: GO if >= 25% of CPU in walk/stat/hash/copy), install-cold t50
Stack 0cee8358 = f251f785 (#5330 + W follow-up) + #5333 head 77f79776 + main a235f590 (has #5325). Harness perf-research/arch-sol-attr (leaf-kind classifier; auth via perf-research/arch-sol-attr-auth = + JH danielmeppiel-perf-auth). One profiled run per cell; shares of total CPU (self + children).
| cell | gate (stat+realpath+walk+hash+file I/O) | top kinds | verdict | run |
|---|---|---|---|---|
| anon L | 1.77 of 4.67 s = 37.8% | python_other 1.87, file_io 0.61, realpath 0.47, stat 0.43, yaml_emit 0.43 | MET (small in seconds) | 45386165930 |
| anon W | 15.16 of 20.98 s = 72.2% | realpath 7.20, stat 4.20, file_io 2.92, python_other 2.86 | MET | 45386215481 |
| auth L | 4.11 of 17.53 s = 23.4% | git children 6.45 (37%), python_other 4.21, file_io 1.39 | NOT MET | 45386188770 |
| auth W | 10.17 of 41.56 s = 24.5% | git children 25.20 (61%), realpath 5.30, stat 2.43 | NOT MET | 45386235503 |
YAML at this stack: emit dominates (L emit 0.43 vs parse 0.10 s; W 0.81 vs 0.55 s); the C loader for APM-written docs is already in f251f785; #5325 is in.
#5333 isolation on Windows: f251+main without #5333 (1ba64b71) vs with (0cee8358), 6 pairs: t50 1.010, t10 0.989 (45387959362) -> neutral; the no-5333 profile shows the same realpath 7.05 s / stat 3.89 s (45387981304). Red flag withdrawn: F7's W 1.9 s was the containment owner family; the realpath leaf spans all callers (38,884 calls/install).
Readout: Windows anonymous cold is 54% realpath+stat (11.3 s): the native/kernel path-resolution lever (step 9, or a Windows analogue of #5333) is the largest single lever left on W; on auth journeys git children still dominate (B1 does not cover them), so a native core alone does not pass the gate there.

## F9 (13:5x CET) Windows path resolution: ceiling vs form (B), install-cold, base 0cee8358, 6 pairs
CEILING (never ships; per-command realpath memo = what roots-once + derived components could remove; 9d23ade6): W anon t50 0.728 (19.27 -> 13.93 s; 45390109666) and 0.716 (19.78 -> 14.35 s; 45390308062), t10 0.815/0.826; L anon 0.957 (45390183634); L auth 0.977 (45390209514); W auth not run (request slip). Profile 45390239176: realpath calls 38,884 -> 2,605.
FORM (B) (W3d B1-B5; exact Path.resolve analogue: one _getfinalpathname per call, \\?\ strip proven for plain Win32 paths <260, no trailing dot/space, reserved names, ':' or ~digit; missing leaf or any error -> Path.resolve; also skips Path.resolve's loop-check stat, exact because an opened path cannot loop): 7af4bc00 vs 0cee8358: W anon t50 0.948 (19.79 -> 18.67 s, -1.1 s), t10 0.970 (45390633875); W auth t50 0.993 (45390685826). Differential SHA 4f6cb937: 0 errors (45390597410). Profile 45390708927: os.path.realpath calls 38,884 -> 10,153 (74% fast-path hit), realpath leaf 7.20 -> 2.26 s, stat 4.20 -> 3.37 s; fallbacks are missing leaves (_getfinalpathname_nonstrict 0.61 s), excluded by W3d B3.
Readout: (B) keeps ~21% of the ceiling (-1.1 of -5.3 s): each fast call still pays one CreateFileW. Most of the W gap needs form (A) (roots once, held handles, components validated, A1-A9) or IFP's call-count cut (~13k calls) stacked. Auth journey ~0 (git children dominate).

## F10 (13:3x CET) (B) over #5354 parks; form (A) cost model passes its kill rule
- (B) increment over #5354 (+#5333) base 23989789 -> 1b307ec2, 6 pairs: W anon t50 0.974 (gate <= 0.97: FAIL by 0.004; 45392847051), L 1.000 (45392890045), M 0.964 (45392931465), requests identical, differential 0 errors (45392817168). -> (B) alone PARKS.
- Form (A) cost model on (B) (9a118be2 vs 7af4bc00; parent dir resolved once per command = the cost of holding its handle, + one lstat/reparse check per call; NOT yet holding without FILE_SHARE_DELETE): W anon t50 0.888 (18.71 -> 16.50 s, -2.2 s), t10 0.914 (45393113133); W auth t50 0.969 (45393138972). Differential 0fae581b: 0 errors W t10/t50 (45393089650). Profile 45393173040: realpath calls 10,153 -> 7,985, realpath leaf 2.15 s, stat 3.18 s.
- Combined (B)+(A model) vs 0cee8358 (product): W anon ~0.842 (VP condition <= 0.85 met on the model; ceiling 0.716-0.728). Increment over (B) 11.2% >= 5% kill line -> A continues to a faithful build.
- W AUTH ceiling cell (memo 9d23ade6 vs 0cee8358): 0.908 (41.99 -> 38.28 s; 45392598445).

## F11 (13:5x CET) Faithful form (A) + (B) on the real stack: VP COMMIT GATE NOT MET -> (B) and (A) both PARK
Faithful A (06e728ad on base 23989789 = #5354 + #5333; differential 066d3e23): parent dirs under the project root held via CreateFileW (FILE_SHARE_READ|WRITE, no DELETE; BACKUP_SEMANTICS|OPEN_REPARSE_POINT; reparse attribute + final path from the same handle); apm_modules/.git/caches never held (A7). 6 pairs vs 23989789:
| gate | result | run |
|---|---|---|
| W anon cold t50 <= 0.90 | 0.996 (40.29 -> 39.61 s); t10 0.990 | 45395421701 |
| W auth <= 0.97 | 0.988 (37.13 -> 36.92 s) | 45395466272 |
| L <= 1.010 | 1.001 / 1.001 | 45395511676 |
| M <= 1.010 | 0.950 / 0.916 | 45395544902 |
| differential 0 errors | 0 errors W anon t10/t50 + auth t50 | 45395584369 / 45395624121 |
| A7 cell: no sharing violations on APM's own renames/deletes | 0 errors on W uninstall, prune, update, install-warm, install-noop (differential SHA) | 45395656307 |
| requests identical | 201->201, 251->251 | above |
Why the model (0.888 over B on 0cee8358) did not carry over: (1) the faithful form only holds project-tree parents (A7 excludes apm_modules, .git and caches, where most resolves on this base land); (2) the 23989789 base has no B1 (#5330), so W cold is 40 s with 674 git spawns and path work is a smaller share; (3) #5354 already removed ~13k calls. The memo ceiling stays 0.716-0.728 on 0cee8358 but it is not reachable faithfully under A7 without holding cache/apm_modules dirs that APM renames.
Verdict per VP rule: no commit; (B) and (A) park on perf-research/arch-win-paths(-on-5354) with these numbers. Re-open only with B1 landed and an A7-compatible way to hold apm_modules package dirs after their final rename.

## F12 (15:3x CET) Windows paths RE-OPEN (A7-compatible held walk + form B): PARKED at the gates
Base 73519c33 = main 784c9dae (has #5330) + #5351 head 1d4df800. Branch perf-research/arch-win-reopen.
Design per W3d R1-R12: project root and every verified dir below it held (no FILE_SHARE_DELETE); one FindFirstFileExW per remaining component (long name must equal text, no reparse); never hold at/below *staging*, .git, .inc., .tmp; release at robust_rmtree, atomic_land, both _rmtree helpers, before package activation, before lifecycle scripts, atexit; thread-safe table.
Iterations (each caught by the A7 probe / differential, as designed):
- cf821192: activation rename of apm_modules\.apm-resolution-staging\...\replacements\<h> failed WinError 5 (staging dir held) -> every install failed (45404019367 ...).
- 6eeca7cd: children of the staging dir still held + thread re-hold race -> same failure (45406808113).
- f342996c: KeyError race in release_held (parallel resolver threads) (45409679013).
- 49d5edc1 (final): measured form, 6 pairs:
| gate | result | run |
|---|---|---|
| W anon t50 <= 0.85 | 0.877 (19.58 -> 16.96 s), t10 0.914 | 45412645488 |
| W auth | 0.951 (29.42 -> 27.75 s) | 45412680391 |
| L / M <= 1.010 | L 0.988; M 1.051 (code inert on macOS; outside the bar) | 45412743791 / 45412780488 |
| differential 0 errors | FAIL: on update, Path.resolve kept \\?\ for a delete-pending staging dir (CPython's 2nd _getfinalpathname failed with another winerror); the fast form stripped it | 45412613794 (+ 1 install-cold error 45412581461) |
Verdict: park (VP kill rule 16:30: no faithful head meeting W <= 0.85 with 0 differential errors). Inputs for v0.39.1: (1) B2 must also fall back when the target is delete-pending (or call the 2nd _getfinalpathname for staging paths); (2) the held walk needs a registry the activation path owns (holding is safe only after activation, and activation runs in parallel threads); (3) the measured 0.877 shows the shape works (memo ceiling 0.72); the remaining gap is the unheld staging/activation window and per-component FindFirstFile on first visits.

## F13 (17:3x CET) Windows STAT-CUT: attribution, ceiling, v1 (pop: W anonymous + authenticated cold; L/M sanity)
Base 81f096a0 = main d62aebe0 (has #5330 + #5351) + #5352 + #5353 + #5354.
Attribution (counting harness perf-research/arch-sol-attr-stat; clean CPU from perf-research/arch-sol-attr):
- 111,342 stat-family calls per W cold t50 install: stat 88k, lstat 9.9k, islink 4.6k, lexists 3.0k, exists 2.1k, isfile 1.4k, isdir 1.3k (45424863223 anon; auth identical 45424979679).
- By location: apm_modules 53%, deploy 25%, cache 12%, root 6%, staging 1.5%, APM_HOME 1.4%.
- Stat CPU (clean): 3.17 s of 17.16 s anon (45425077173), 2.61 s of 14.41 s auth (45425154240).
- Top callers by calls:
  - resolve_path 27,327 (Path.resolve's loop-check stat; CPU 0.93 s anon / 0.52 s auth). Form B removes it, so it OVERLAPS the Windows Paths engineer.
  - path_security._is_link 8,350 (0.39 / 0.31 s). Containment component walk; the held handles of form A answer it.
  - content_hash._package_files 7,254; deploy_store _prepare/_real_dir 9.8k.
  - A long tail of 1-3k each: find_files_by_glob (nlink check, needs a real stat on Windows), check_collision, portable_relpaths, find_plugin_json, skill_integrator, format_detection, _write_git_dir exists, compute_deployed_hashes, gate._scan_candidates, ledger.record.
CEILING (arithmetic, from the clean profile): non-overlapping stat CPU = 3.17 - 0.93 = 2.24 s anon (13% of 17.2 s), 2.61 - 0.52 = 2.09 s auth (14.5%). Removing ALL of it -> W anon ~0.87, auth ~0.85. A faithful cut cannot remove all of it: find_files_by_glob's hardlink check and the containment walks need real stats. The realistic faithful ceiling is ~0.93-0.95.
V1 (perf-research/arch-stat-cut b7d06ef6, 3 callers: _package_files via one scandir walk with DirEntry type data, entry_state from one lstat, deploy_store one lstat per dir), 6 pairs vs 81f096a0:
| cell | t50 | t10 | run |
|---|---|---|---|
| W anon | 0.979 (13.48 -> 13.35 s) | 1.002 | 45432230036 |
| W auth | 0.980 (14.77 -> 14.10 s) | - | 45433094704 |
| L anon | 0.980 | - | 45432347433 |
| M anon | 0.972 | - | 45432422382 |
- Differential SHA 60f8ad7e: 0 errors on W anon t10/t50 (45432398212) and on the W probe uninstall/prune/update/warm/no-op (45432184849).
- Calls 111,342 -> 97,485 (45432297743). Requests identical.
W3d (row w3d:ruling:statcut): OK-with-conditions S1-S7 (junction reparse check from find data, fstat checks, mkdir without exist_ok, CI differential cells).

## F14 (18:0x CET) Stat-cut v2 increment on the Windows Paths engineer's stack: ~1-2%; handed off
v2 (W3d S1-S6) on E f6245391 = 60fbc0af + #5352-#5354; measured 36dccfc2, differential 28cae5d5. 6 pairs:
- W anon t50 0.986, t10 0.983 (45439730141).
- W auth 1.010, noise (45439813597).
- L 0.979 (45439893328); M 0.982 (45439964732).
- Differential + probe: 0 errors (45439534928 / 45439632454).
- Stat calls per W install 111k -> 79k (45440063436).
Handoff: files/statcut-handoff.md. Fold into the Windows Paths PR if the line budget allows, else drop.

## F15 (19:15) Boot floor + import lever (pop: anon; boot is population-independent) -- files/boot-floor.md
- Boot = ~0.45 s L / ~0.55 s W (interp 0.01/0.02, imports 0.39-0.47/0.45-0.55, click 0.01). Runs 45452267627, 45454873425.
- Imports = 8.2-8.9% of L cold t50, 4.5-5.2% of W (same-runner). B2's 2.6% stale.
- Lazy re-export prototype cf8c2d96: 25 modules / 30 ms avoided; paired t50 L 0.988 [45454557177], W 0.995 [45454614998] -> KILL.
- zip/pyc/frozen: no steady-state win; frozen apm_cli unreachable for pip/uv. pyc-missing tail +0.5-0.7 s/run.
- A/A same runner: rtt 0 vs 30 and counts off/on move CPU <= 3% [45454873425]; cross-job absolute varies 25-40%+; bench absolute ~1.5x direct journeys run (open).

## F16 (19:25) Bench calibration -- files/calibration.md [45457579415]
- Same VM: bench path / direct path = 0.96-1.04 (4 L + 4 W VMs). pyc ruled out (written on first run; 497 pyc).
- Cause of 1.5x: runner CPU class. W main t50 8.45 (EPYC 9V45) / 13.2 (9V74) / 17.2-17.7 (7763); L 6.1 (9V74) / 8.0-8.3 (7763).
- Bench shards land on different classes; the pooled base p50 is a class mix. Paired ratios valid; absolutes need a class or a yardstick.

## F17 (19:40) Rest bucket attribution + rest-cut PARK -- files/rest-attribution.md
- Rest on e62f520f t50: L 1.1-1.25 s, W 2.6-2.8 s; owners: imports 0.33/0.5, config+manifest reads 0.1-0.17/0.56-0.68, unsampled 0.2-0.37, validation 0.12-0.4, cache bookkeeping, content-scan re-reads.
- Rest-cut prototype e2cf95e4 (negative config cache + APM-home resolve memo): L 0.999/0.997 [45457830059], W 0.995/1.005 [45457855964], W default 0.994/0.994 [45458015183] -> PARK (< 3%).
- W profile self-time on small hot functions overstates (0.36 s attributed, ~0 measured).

## F18 (19:55) Single-pass integrate ceiling: KILL -- files/single-pass.md
- Upper bound A 306874b7 (bytes+hash memo): L 1.002, W 0.999 t50 [45461174996, 45461225856]; B d345643 (+walk, verified-read, journal fd): L 0.993, W 0.987 [45461604679, 45461641952]. Kill rule < 15% L.
- Engagement proven by profile 45462440650: reads/walks are already one per path; read leaves ~6% L total.

## F20 (01:30, 25 Nov) Records sub-block split: no faithful car -> v0.40 headline
- Separate deletions (W/L): journal 0.990/0.991, entry_state 0.993/1.005, ledger.record 0.999/0.977, deploy_store put 0.981/0.971 [45512251179.. 45512535211].
- Combined re-run tonight: W 0.968 [45514773300], L 0.953 [45514804328] (yesterday's 0.905 overstated). Mostly downstream knock-on; batching cannot remove it.
## F21 (01:30) Locked-warm: the diff-apply fast path APPLIES (667 files) and costs more than the full pipeline
- Deleting it (b7275b8d): locked-warm W 0.885 [45513481474], L 0.919 [45513512058]; warm control 1.009/0.993.
- Trace 45516588105 (L+W): locked-warm "applied a verified diff (667 files)"; warm "full run (no lockfile)".
## F22 (01:30) Warm cache-hit remainder on main 1e4a208e + #5410 (ec258a86 -> stub 34bd5f70)
- L warm 0.978 / warm-auth 0.987; spawns 64 -> 15 / 67 -> 16 [45515065061]. The remainder is the ls-tree link plan (git_sparse._plan_tracked_symlinks). W pending (45515035586 schannel, 45515126517 openssl).
- Hooks/MCP fully enabled (tree-50-exec): hooks 0.975, MCP 0.992 W. sklinks ~0.

## F23 (02:00, 25 Nov) Locked-warm early-decline car #5414: KILLED on current main
- vs main 1e4a208e: install-locked-warm L 2.221x worse (4.31 -> 9.57 s) [45517792540]; with #5383: W 2.27x / L 2.21x [45517942152 / 45517968159].
- Trace [45519638267]: main serves fresh clone with "applied a verified diff (667 files)", which is now ~2.2x cheaper than the full pipeline. The F21 upper bound (e62f520f) is stale on main.
- On main, locked-warm (4.3 s L) < warm (7.8 s L): the locked-minus-warm regression is gone.
- Differential fast vs full on main [45518143217]: lockfile + 1,441 project files byte-identical; only the random installation identity + bound receipts differ.
- LESSON: re-measure a deletion upper bound on CURRENT main before building the car.
## F24 Warm cache-hit remainder (main + #5410): W schannel warm 0.960 / auth 0.957 / update 0.952 [45515035586]; OpenSSL 0.961 / 0.966 / 0.958 [45515126517]; spawns 66 -> 16. Lead ruling: no car, design doc.

## F25 (02:20, 25 Nov) Phase map re-grounded on main 1e4a208e -> files/main-phase-map.md
- integrate W 0.499 / L 0.551 (stack 0.415 / 0.402); skills 0.854/0.871; instr 0.927/0.947; prompts 0.944/0.956; agents 0.962/0.973; shared remainder ~19% (stack 27-33%); lockfile+ledger+journal 0.789/0.770; policy 0.961/0.980.
## F26 (02:29) no-op lead refuted (bench no-op = first unchanged install after full install; proof run) [45523138212]; frozen-warm kept-mcp decline = trace-harness sequencing artifact; deletion 0.995 L [45524432672].

## F27 (03:05, 25 Nov) Materialize-once is a lane on main: the apm_modules copy + re-verify = 15-20% of cold (L/W/M), 16-21% warm
- hardlink+verify: cold W 0.832 / L 0.832 / M 0.850; warm W 0.838 / L 0.827. hardlink no-verify (M2 bound): cold 0.802 / 0.817 / 0.856; warm 0.833 / 0.794. Runs 45527826943..45528022070.
- Per-file Python copy machinery, not bytes (~1.4 ms L / 3.5 ms W per file). W3d: M2 (fetch-to-final-tree) and M1 (reflink) OK under MS1-MS7; M3 hardlinks rejected.
## F28 (03:05) MCP services deletion on main 1.005 / 1.005 [45527692033 / 45527709409]: probe discrepancy closed (concurrent threads).
## F29 (02:50) Section 1 drafted: files/design-sec1-floor.md (probe run 45526028119; reachability 0.992-0.994).
