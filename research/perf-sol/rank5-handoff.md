# Rank 5 handoff: git config "read once" for clone envs (for Journey Hunt)

Branch `perf-research/arch-config-once` from main d849de7b. Head 28f4e939: W3d D1/D4/D7 folded in, plus BETS cells 374e631d cherry-picked as 37165f89. Touches one file: `src/apm_cli/utils/git_env.py`. Src diff +151 / -0, of which ~25 lines are the differential and docstrings. Does not touch B1 code.

## What it changes (file:line at 28f4e939)
1. `git_env.py:1675-1697` `git_clone_env`: when the throwaway probe repo would be freshly created, try `_config_once_clone_env` first. It falls back to `_probed_clone_env` (today's code, unchanged, `:1737`) on None.
2. `git_env.py:1699-1713` `_needs_probe_repository` (W3d D1/D2): returns True on any include.path / includeIf.*.path in any scope, and on any local/worktree-scope entry (the repo-less probe ran inside some repository).
3. `git_env.py:1715-1735` `_config_once_clone_env`: reads the parent and child repository-less snapshots. Both are memoized per command by the #5283 `git_config_memo` (D8: no new cache). If neither needs a probe repo, it returns `git_network_env(remote_url, overrides, git_dir=None)`. Why it is equal: a fresh `git init --no-template` + `git remote add origin` writes only core.* and remote.origin.* locally, and `_materialize_git_config_snapshot` keeps only http.* / url.*.insteadOf from local scope.
4. `git_env.py:904-966` `_simple_http_url` / `_inprocess_header_group`: Git's http.<url>.extraHeader urlmatch done in process, for plain http(s) URLs only. Rules:
   - The generic `http.extraheader` scores lowest.
   - A URL key matches when scheme, host and port are equal (default ports normalized) and its path is a prefix at a '/' boundary. The longest path wins.
   - Returns None, so the caller spawns `git config --get-urlmatch` as today, on: user info, wildcard, %, ?, #, backslash, dot segments, //, a tie between two spellings, or case-folded spellings of one key (D4).
5. `git_env.py:968-986` `_urlmatched_header_group`: uses the in-process result when it is not None, else `_spawned_header_group` (`:988`, today's spawn code).
6. `git_env.py:899` `_CONFIG_ONCE_DIFFERENTIAL` (D7): under APM_DEBUG both paths run, and any mismatch raises (env dict equality for 1-3, group equality for 4-5).

## W3d rank-5 ruling (row w3d:ruling:rank5-config-once, D1-D8)
- D1 local/worktree entries force the probe repo: DONE (item 2). Cell: TODO (JH). BETS cells cover includes, not a cwd-inside-a-repo case.
- D2 include refusal in all scopes: DONE (item 2); BETS cell covers include / includeIf gitdir / hasconfig.
- D3 unwritable target: the config-once path creates nothing, so the clone itself fails non-zero. Error text changes; cell TODO (JH).
- D4 case-folded spellings return None: DONE (item 4). Cell TODO (JH).
- D5/D6 refusals kept; empty-value resets are inside the differential: kept as is.
- D7 seeded >=200-case differential as a CI cell under APM_DEBUG: flag DONE (item 6); the 200-case seeded cell is TODO (JH). BETS has 18 shapes.
- D8 #5283 memo only: DONE.

## Cells (BETS, test-only, 374e631d -> 37165f89)
`tests/integration/test_git_config_once_cells.py`: 23/23 pass on Linux (run 45371778626) and Windows (run 45371811988). The "failure" status on those runs is the bench Report job, which had no bench output; pytest ran through perf-research/arch-cells-runner.

## Evidence (install-cold, paired CPU p50, base main d849de7b; B1 is off at that main, which stands in for non-github hosts)
| head | OS | t10 | t50 | git spawns t50 | run |
|---|---|---|---|---|---|
| f47c62c5 | Linux, 6 pairs | 0.960 | 0.929 (17.77 -> 16.84 s) | 674 -> 365 | 45371142590 |
| f47c62c5 | Windows, 6 pairs | 0.837 (9.92 -> 8.16 s) | 0.842 (44.64 -> 37.59 s) | 674 -> 366 | 45371171795 |
| 3594c07b (differential ON) | L / W, 3 pairs | 0 errors | 0 errors | 772 (both paths) | 45371071882 / 45371110863 |
| 28f4e939 (W3d D1/D4/D7) | L / W, 6 pairs | pending | pending | - | perf-bench/28f4e939-* |
Spawns by subcommand at t50 (profiles 45371202819 L / 45371227970 W), config + remote + init:
- `--show-scope --list` 104 -> 4; `--get-urlmatch` 108 -> 0; `--get-regexp` 7-8 unchanged.
- `git remote` 52 -> 2; probe `git init` 50 -> 0.
- Windows child CPU for these: ~5.3 s -> ~0.2 s.
Overlap: on github.com with B1 (#5330) on Linux most of this is already gone. The win stands on Windows until B1-win lands, and on every non-github.com host (B1 is github.com-only).
