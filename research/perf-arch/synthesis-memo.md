# PERF architecture research -- synthesis memo (FINAL, to VP for the regent 17:00)
Team: Lead d7d7be0d; SoL researcher 2ec55fc5 (findings F1-F12 + EOD table); Bets researcher e7b91082. Zero local runs all day; every number from CI-bench runs.
Scope: `apm install` cold fresh (CEO focus), then authenticated installs (regent 11:48: first-class). Every number = paired CPU incl. children on the CI bench, 6 pairs unless stated, with run IDs; counts are the oracle; W3d co-owns every mechanism (rule 26). Populations named per row.

## 0. Headline (end of day, verified)
Cold fresh install t50, paired CPU incl. children, (a) morning base 3c62c90a -> (c) landing stack 8eca5c07 (main dbfa810f + #5330 #5351 #5352 #5353 #5354 + #5333):
| OS | anonymous | authenticated | runs (a->b / b->c) |
|---|---|---|---|
| Windows | 0.388 = 2.58x (b->c 29.65 -> 14.63 s) | 0.345 = 2.90x (32.64 -> 13.97 s) | 45398398272 / 45398448435 |
| Linux | 0.434 = 2.30x (10.73 -> 7.13 s) | 0.421 = 2.38x (12.23 -> 7.17 s) | 45398476173 / 45398505590 |
| macOS | 2.46x | 2.66x | 45398552025 / 45398594722 |
NOTE: stack (c) includes #5352/#5353, which are PARKED to v0.39.1 and will NOT be on main at the tag; the on-main-at-tag number comes from the post-landing 15-pair runs.
Spawns 674 -> 17 (anon), 815 -> 19 (auth); requests halved. Gap to floor: W ~9.5x, L ~10x. Remaining: W realpath 31-33% + stat ~3 s; L pure-Python orchestration 35%. Detail: SoL eod-sol-table.md.

Linux remaining (stack 8eca5c07, cold t50, profiles 45398734035 anon / 45398775062 auth): the '35% pure-Python orchestration' is mostly REALPATH in disguise -- #5333's _kernel_realpath frame 1.78 / 1.95 s + leaf 1.29 + missing 0.13 = ~3.2 s (~33% of L cold, 7.7k calls x ~0.4 ms). Next: YAML emit (yaml_to_str) 1.10 s; journal append_private 0.33-0.40 s; parse_primitive_file 0.20 s. => IFP after #5354: cut the COUNT of path resolutions on Linux too; then YAML emit; then the journal (S5 car).

## 1. Physics (where we stand vs the speed of light)
- Floor (0 git children, 2 requests/pkg, ~28 writes + ~27 hashes/pkg): L t50 ~0.7 s, W ~1.5 s. Morning main: L 15.0 s (21x), W 48.5 s (32x) [45353723802 / 45353749594]. Bytes already at floor (0.59 MB at t50).
- Unit costs measured on runners [45359600677]: git spawn 1.1 ms L / ~21 ms W; O_EXCL write 16 / 67 us vs today's checked write 75 / 223 us; realpath 33 / 53 us; SSLContext 12 / 94 ms; python boot 9 / 19 ms.
- After today's levers, git children are ~0 on anonymous installs (L 0.03 s, W 0.30 s). What is left is per-file Python work (containment/realpath, YAML emit, discovery) -- and, on Windows, PATH RESOLUTION (realpath 7.2 s + stat 4.2 s = 54% of anon cold t50) [45386215481]. Authenticated installs are still dominated by git CLI children (37% L / 61% W) [45386188770 / 45386235503].

## 2. Ranked levers -- measured, with security invariants and status
| # | lever | population | measured (cold t50) | invariants kept (W3d) | cost line | status |
|---|---|---|---|---|---|---|
| 1 | B1 in-process pinned fetch (#5330) | anon github.com | L 0.719, M 0.500 [final 06f5fe70] | F1-F12, R1-R6, breaker (B)(C) | ~5 ms/pkg in-process, 0 spawns | MERGED |
| 1w | B1 Windows follow-up (#5351) | anon github.com | W 0.444, spawns 678 -> 27 [45369015657]; L 0.995; final head d1b89196 W cold 0.541 [45409984669] | W1-W6, G1-G7 | 1 SSLContext/process | FULLY GATED, lands by 17:00; discloses L cold +1.3%, W warm +1.1% |
| 2 | A' authenticated in-process fetch (#5353) | auth github.com | W 0.504 (50.74 -> 25.66 s) [45378994721], L 0.708 [45375435538] | A1-A10 | token only in the call frame, per-request header | PARKED to v0.39.1 (regent 15:07; missed the 15:30 line) |
| 3 | F3 config-once (#5341) | auth/enterprise, all hosts | W 0.826 (-7.7 s) [45377500317], L 0.918 | D1-D8 (urlmatch differential, include -> probe) | 262 spawns -> 1 | MERGED |
| 4 | git-path 2b+3 (#5352: pin from .idx, ls-tree once) | auth, all hosts | W 0.922 [45377540762], L 0.972 at prototype base; on current main 9-pair L cold-auth 0.989 [0.960, 1.014], spawns 367 -> 367 (#5318/#5341 took its ground) [45406754966] | V1/V2/L1/L2 | - | PARKED to v0.39.1; re-judge on a non-github host |
| 5 | realpath round 1 (#5333, Linux kernel resolve) | all | L cold 0.903; W neutral 1.010 [45387959362] | K1-K6, C1-C8 | - | MERGED via train #5355 f4f1ad2c |
| 6 | B3 keeper: materialize from verified bytes | anon | L 0.932 [45358796767], M 0.934 [45367057630] | verify-by-construction for own B1 trees only | ~80 src lines | F&C after #5330 |
| 7 | YAML identity (#5325) | all | L warm 0.906 | C loader, byte-identical dumper | - | MERGED |
| 8 | #5361 lockfile via libyaml when bytes match | all | cold/warm t50: L 0.908 / 0.922, W 0.976 / 0.971, M 0.980 / 0.943 [45408471475 / 45408501021 / 45408524737] | byte-identical emit or fall back | - | pending H8 + admin |
| 9 | #5354 realpath round 2 (resolve fewer times, IFP) | all | W .968 / .974, M warm .923, L neutral | rank-1 family | - | pending H8 + admin (N1 swap a55dbea3 reverted) |
Stacked projections: anon W ~0.44 x 0.93 -> ~2.4x; anon L ~1.75x; auth all-hosts W ~0.76 (F3 + 2b/3), auth github.com W ~0.5 with A'.

## 3. Killed or parked today (evidence, so nobody re-tests without a new reason)
- B2 interpreter/imports: 2.6% of cold [B2 sizing] -- parked.
- B3 remainder (hash seeding/pin re-seal): 1.011 [45365230317]; re-seal NO by W3d.
- Hash-once standalone: 0.51 s only (bytes cheap; per-object Python is the cost).
- git-path lever 4 (in-process checkout .git): W 1.178 (Windows re-fetch) [45382610173] -- killed.
- N1 plan-then-write: 0.933 L / 0.948 M [45384894338 / 45384946472] -- killed as headline (95% of 're-walk' lstat was realpath).
- Materialization fan-out: target fan-out is 1x; faithful 0.964 L / 0.948 W [45387750512 / 45387773859] -- killed (writes are ~0.08 ms each; deploy CPU is Python, not I/O).
- Small-cars train (S1 + S5 + GC): cold L 0.960 / M 0.954 / W 0.966 [45389129843 / 45389161218 / 45389181318] -- under the 10% bar; parked as a v0.39.1 WARM-journey train (~6% warm on every OS) with B1 pack+idx.
- Native core (walk/hash/copy): gate met only where realpath dominates (anon W 72%, anon L 1.77 s only); NOT met for auth (23-25%) -> no native walk/hash/copy core; see bet R1.

## 4. Next bets (ranked by seconds left on the table)
| # | bet | population | seconds at stake (t50) | invariant + fastest faithful form | cost line | kill date |
|---|---|---|---|---|---|---|
| R1 | Windows per-call path resolution (roots once + validated components + handle containment) | all, Windows | CEILING W anon 0.716-0.728 (-5.3 s) [45390109666]; faithful re-open (post-#5351 base, holds after final placement) W anon 0.877 (-2.6 s) [45412645488], W auth 0.951; misses <= 0.85, M 1.051, differential not clean (delete-pending \\?\ prefix on update) | W3d R1-R12 (FindFirstFileExW per component, hold==proof, release before lifecycle scripts/activation, never hold staging) | 1 handle per held dir | PARKED 15:34 CET -> v0.39.1: form B falls back on delete-pending; holds owned by the activation path. A7 probe caught 3 real defects |
| R2 | Git-path fetch shape (depth-1 by-SHA bare) | auth + enterprise, large history | real-history fixture L 0.985 / W 0.935 even with a double fetch; v1 tree-50 regressed (shallow bare breaks clone --local --shared -> silent 2nd fetch); checkout dominates git CPU -> ~2-7% | W3d D1-D8 | 1 shallow fetch/pkg | CLOSED 13:5x: v2 real L 2.568 (partial+shallow makes sparse checkout lazy-fetch blobs one by one) [45394357668]; no shape beat today's; git checkout dominates |
| R2b | Git-path checkout + verify from the pack (lever 2a + C) | auth token-first, all hosts | W 0.862 (27.56 -> 23.47 s) [45400831263], L 0.973; spawns 365 -> 165 | W3d C1-C12 (keep clone --shared --no-checkout; links/gitattributes -> git; staged 0700 + atomic_land) | 0 checkout/ls-tree/cat-file spawns | CONFORMING f0e79d13: W 0.859 [45404312946], L 0.963; C6/C8/C11/C9 cells green; handed to L5, v0.39.1; subdir partial deps not engaged (C4) |
| R2c | Lever S: monorepo SUBDIR deps on the git path (pin trees depth 1 + exact cone blobs in ONE request, in process) | auth + enterprise, monorepos | today per repo: full-history partial bare (microsoft/apm 22.5 MB, 114k objects, ~2.1 s git CPU; awesome-copilot 5.4 MB) + 1 lazy fetch/dep; S: ~29 MB -> ~1.5 MB (~20x bytes), git children ~3.3 s -> ~0; PAPER ceiling ~40% of real-repo L cold [45407084343] -- NOT measured | W3d S1-S12 (#4970 admission first; every object re-hashed from the pin; missing/unrequested blob refuses; B1/A' gates; C's writer; cone differential; .git = B1 shape + shallow, no promisor; refusal -> git) | 1-2 requests/pkg, 0 spawns | KILLED for today; TOP v0.39.1+ bet, owner Bets; FIRST deliverable = measured faithful ceiling on real-repo fixtures (no build before it); gated on token rotation |
| R3 | Authenticated listing in-process (LA1-LA5) | auth github.com | ~50 ls-remote spawns (~1 s W) | LA1-LA5 | 0 spawns | own post-cut PR |
| R4 | Warm-journey train (S1 + S5 POSIX + GC + B1 pack+idx) | all, warm | ~6% warm every OS | S1/S5/K1-K6, GC named process-wide | <150 lines | v0.39.1 |
| R5 | Walk residual 'walk once' (IFP) | all | <= ~0.7 s L | lstat semantics, memo cleared on write | - | IFP sizes after #5346 |

## Blocker
APM_BENCH_GITHUB_TOKEN (bench secret) is INVALID ('Invalid username or token') [45399550619]: no authenticated real-repo number is possible until an admin rotates it; required before any authenticated release claim.

## 5. Lessons for Atlas (proposed rows)
1. Engagement is the killer of fast paths on Windows: B1 (system gitattributes), A' (managed header position), lever 4 (later reader) -- every fast path needs a Windows engagement cell + spawn oracle before any number is quoted.
2. Unit costs differ 20x by OS (git spawn 1 vs 21 ms; SSLContext 12 vs 94 ms; realpath 33 vs 53 us x 39k): rank Windows levers on Windows numbers.
3. A paper floor/attribution must separate overlapping families (realpath counted as 'discovery' lstat); re-cut after every landing.
4. Writes are cheap, Python per-file work is not: I/O fan-out levers die; owner-count levers (resolve once, list once) live.
5. Silent fallbacks hide broken fast paths (fetch-shape v1: 0 errors, +50 fetches). PROPOSAL TO QL: every fast path exposes an engagement counter and every fast-check row asserts it (spawns/requests as the oracle; '0 errors' alone never passes).
6. Security co-ownership at design time worked: 12 W3d rulings today, each with the cheapest equivalent form (e.g. helper-present + anonymous-first = same credential behaviour), none blocked a lever.

## 6. Owners and next steps (v0.39.1 plan input; team stands down after this memo)
| item | owner | next step / gate |
|---|---|---|
| Post-queue gains (headline sec. 0) | VP -> regent | direct 15-pair runs after the queue lands for release notes, anon and auth reported separately |
| Lever C + 2a (git-path pack checkout, auth whole-repo deps) | L5 (builder), Bets (questions) | leverc-handoff.md; needs #5351 (re-judge after #5352 parked); W3d C1-C12 cells green at f0e79d13; full H8 + 15-pair verdict at PR head |
| Lever S (monorepo subdir deps) | Bets | FIRST a measured faithful ceiling on real-repo fixtures; no build before it; blocked on APM_BENCH_GITHUB_TOKEN rotation |
| Windows paths (form B + activation-owned holds) | SoL design input -> IFP | v0.39.1: B falls back on delete-pending targets; holds owned by the activation path; gate W anon <= 0.85, differential 0 errors, A7 probe clean |
| Linux remainder (realpath count 7.7k, YAML emit 1.1 s, journal 0.4 s) | IFP after #5354 | re-profile at post-queue main first |
| Warm-journey train (S1 + S5 POSIX + GC + B1 pack+idx) | F&C (pack+idx), Bets (cars) | judged at combined head, >= ~10% warm or stays parked |
| Bench token rotation | regent / admin | blocks all authenticated real-repo numbers |
