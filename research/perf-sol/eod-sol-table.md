# EOD speed-of-light table v2: `apm install` COLD FRESH (PERF-SOL, 14:2x CET)

## The three points
- (a) Morning base: 3c62c90a (#5283 merged).
- (b) Current main: dbfa810f. Since (a), main gained:
  - #5318 verify pinned commits from one shared ref listing (a5c75a9d).
  - #5325 lockfile identity via the memoized parse (f3713108).
  - #5341 git config read once (f25aee4d).
  - #5132 was already in. #5333 is NOT on main.
- (c) Landing stack: perf-research/arch-eod 8eca5c07 = main + #5330 in-process pinned fetch (5959b32f) + #5351 in-process fetch on Windows (a14640d1) + #5352 pack-index pin checks and link plan once (d4f7712e) + #5353 authenticated in-process fetch for github.com (54cff3be) + #5354 resolve roots once (b6cd3971).
  - #5354 needed one trivial conflict resolved (lazy root + resolve_path in plugin_root_render).
  - The stack also carries #5333 at 77f79776, through #5330's history.
- All merges are clean otherwise.

## Method
- Paired CPU = user+sys of apm plus every child process, p50 over 6 interleaved pairs (2 per runner x 3 runners).
- Harness: JH danielmeppiel-perf-auth (has both journeys: `install-cold` = anonymous public github.com, `install-cold-auth` = token-first authenticated).
- a->c is the product of the a->b and b->c paired ratios.
- Absolute seconds are from the paired run that measured that side; runner classes vary about 20-40% between runs, so trust the ratios over the absolutes.
- Run IDs:

| OS | a->b run | b->c run |
|---|---|---|
| Windows | 45398398272 | 45398448435 |
| Linux | 45398476173 | 45398505590 |
| macOS | 45398552025 | 45398594722 |

## Table (t50; t10 in brackets)
| OS | population | (a) CPU s | (b) CPU s | a->b paired | (c) CPU s | b->c paired | a->c (x) | spawns a / b / c | requests a / b / c | gap (c) to floor |
|---|---|---|---|---|---|---|---|---|---|---|
| W | anon | 48.06 | 35.86 | 0.747 [0.786] | 14.63 | 0.519 [0.620] | 0.388 = 2.58x [0.487] | 674 / 316 / 17 | 201 / 201 / 101 | 14.6 vs 1.5 s = 9.8x |
| W | auth | 53.20 | 39.33 | 0.739 [0.774] | 13.97 | 0.467 [0.555] | 0.345 = 2.90x [0.430] | 815 / 365 / 19 | 250 / 250 / 100 | 14.0 vs 1.5 s = 9.3x |
| L | anon | 14.75 | 9.52 | 0.639 [0.750] | 7.13 | 0.679 [0.801] | 0.434 = 2.30x [0.601] | 673 / 313 / 16 | 201 / 201 / 101 | 7.1 vs 0.7 s = 10.2x |
| L | auth | 16.42 | 11.19 | 0.701 [0.726] | 7.17 | 0.600 [0.678] | 0.421 = 2.38x [0.492] | 815 / 363 / 19 | 250 / 250 / 100 | 7.2 vs 0.7 s = 10.2x |
| M | anon | 18.41 | 16.68 | 0.805 [0.810] | 10.59 | 0.505 [0.560] | 0.407 = 2.46x [0.454] | 671 / 515 / 12 | 201 / 201 / 101 | 10.6 vs ~0.9 s (est) = ~12x |
| M | auth | 18.90 | 17.56 | 0.791 [0.802] | 11.84 | 0.475 [0.476] | 0.376 = 2.66x [0.382] | 814 / 561 / 14 | 250 / 250 / 100 | 11.8 vs ~0.9 s = ~13x |

Other counts at t50, anonymous (counters harness, runs 45398892203 L / 45398920477 W, b -> c):
- File writes rose: L 6,539 -> 8,382 and W 5,909 -> 7,762. The in-process fetch writes each `.git` dir and the payload as files; git used to write one pack.
- Hash objects ~9.0k on both. Scandir 8.9k -> 8.3k.
- Realpath calls per install at (c): L 7,726 (from ~39k at (a)/(b)); W 34,903 (from 39,224 at (b)).

## Top remaining buckets at (c), one profiled run each (tree-50, leaf kinds + check families)
| OS / population | total | #1 | #2 | #3 | run |
|---|---|---|---|---|---|
| W anon | 15.56 s | realpath 4.88 s (31%) | stat 2.91 s (19%) | file I/O 2.38 s (15%); copies/writes family 1.98 s | 45398632263 |
| W auth | 19.83 s | realpath 6.54 s (33%) | stat 3.90 s (20%) | file I/O 2.91 s (15%) | 45398686321 |
| L anon | 9.65 s | pure-Python orchestration 3.34 s (35%) | file I/O 1.37 s (14%) | realpath 1.29 s (13%); YAML 1.12 s | 45398734035 |
| L auth | 9.70 s | pure-Python orchestration 3.58 s (37%) | file I/O 1.37 s (14%) | realpath 1.23 s (13%); YAML 1.09 s | 45398775062 |
- Git children at (c): 0.02-0.34 s on every cell. B1 + #5353 remove the git CLI from both populations on github.com.

## Windows-paths reopen check (VP 14:04: realpath >= 25% of W anon cold CPU on main after #5351 lands)
- (b) main today: realpath 7.86 of 42.09 s = 19% (git children 18.95 s still dominate; run 45398808889). Below the bar.
- (c) landing stack with #5351: realpath 4.88 of 15.56 s = 31%. On auth it is 6.54 of 19.83 s = 33%.
- Once #5351 lands, the reopen condition is expected to be MET on Windows.

## Headline
- Today, cold fresh t50 moved 2.30-2.90x on the landing stack versus this morning: W auth 2.90x, W anon 2.58x, L 2.30-2.38x, M 2.46-2.66x.
- Main alone gives 1.24-1.56x (a->b paired 0.639-0.805).
- What is left sits 9-13x above the speed of light.
  - Windows: realpath + stat (~50%).
  - Linux: pure-Python orchestration (~35%), plus file I/O, realpath and YAML.
