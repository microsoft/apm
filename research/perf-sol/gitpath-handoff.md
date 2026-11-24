# Git-path levers handoff (for L5): 2b + 3 SHIP, 4 PARKED

Branch `perf-research/arch-gitpath-verify`, on config-once 28f4e939 (F3, landing as #5341).
- Ship set = 7b056a1b (levers 2b + 3, differential OFF). The differential SHA is 1231c697.
- Lever 4 is on top at a976850d (measured) / 4a1587e6 (differential). It is PARKED: Windows regresses (below).
- None of these levers touch git_env.py (line cap). Per-file src deltas, 28f4e939 -> 7b056a1b:
  - `src/apm_cli/cache/git_cache.py` +80 / -2 (1488 -> ~1566 lines).
  - `src/apm_cli/utils/git_sparse.py` +54 / -1 (613 -> 666).
  - Both include the differential, about 25 lines in total.
- Recommendation: move `_packed_commit` into a new module `src/apm_cli/cache/pack_index.py` (~70 lines) and keep 3 call-site lines in git_cache.py.

## Lever 2b: "is <sha> a commit here" from the pack .idx (no `git cat-file -t`)
- `git_cache.py:320-391` `_packed_commit(git_dir, sha)` reads idx v2 (fanout + bisect, then the entry type bits at its pack offset).
  - Returns True/False only for a non-delta SHA-1 pack entry.
  - Returns None, so the spawn runs as today, on: loose object, alternates, delta entry, 64-bit offset, other idx version, not found.
- Call sites: `git_cache.py:1036` (`_fetch_tag_only_commit`: skip the probe when the pack holds the commit) and `:1364` (`_bare_has_commit`).
- W3d (row w3d:ruling:gitpath-levers) OK-with-conditions:
  - V1 REQUIRED: any idx error -> None. TODO: wrap the body; add git's idx length check.
  - V2 REQUIRED: any replace ref (refs/replace/*, packed or loose) -> None. TODO.
  - V3: check pack header, object count == fanout[255], pack trailer == the idx's pack checksum. TODO.
  - V4: also refuse http-alternates and MIDX (multi-pack-index). TODO.
  - V5: checkout_verify unchanged. DONE.
  - V6: crafted cells (truncated idx, delta commit, alternates, replace ref); use mmap. TODO.

## Lever 3: tracked-link plan once per commit id (one `ls-tree` per package instead of two)
- `git_sparse.py:303-375`: `_detached_head` reads the detached commit id from `.git/HEAD`, the file `ls-tree HEAD` resolves.
- `_plan_tracked_symlinks` memoizes `_plan_tracked_symlinks_uncached` by (commit id, paths). The plan is a pure function of the commit's tree.
- W3d OK-with-conditions:
  - L1 REQUIRED: use `invocation_memo` instead of the module dict, and return copies. TODO.
  - L2 REQUIRED: no memo when replace refs exist; put the object format in the key. TODO.
  - L3: memo only for a regular-file `.git/HEAD`. TODO (today a symlink HEAD would be read).
  - L4: link writes and containment still run per repo. DONE (only the plan is memoized).
  - L5: cells. TODO.

## Evidence for the ship set (7b056a1b vs 28f4e939, install-cold, 6 pairs, B1 off)
| population | OS | t10 | t50 | spawns t50 | run |
|---|---|---|---|---|---|
| authenticated (JH harness danielmeppiel-perf-auth @3ca6002e, install-cold-auth) | W | 0.956 | 0.922 (29.80 -> 27.62 s) | 414 -> 315 | 45377540762 |
| authenticated | L | 0.997 | 0.972 (17.99 -> 17.52 s) | 415 -> 310 | 45377460191 |
| public github.com anonymous | W | 0.950 | 0.933 (27.91 -> 26.45 s) | 364 -> 266 | 45375572721 |
| public github.com anonymous | L | 0.989 | 0.975 (16.53 -> 16.03 s) | 364 -> 266 | 45375535638 |
- Per lever, git verbs at t50 (auth): `ls-tree` 100 -> 50 (lever 3); `cat-file` 100 -> 50 (lever 2b removes the `-t` probes; verify `--batch` stays).
- Differential SHA 1231c697: 0 errors on L/W t10/t50 (45375475046 / 45375501015).

## Lever 4 (PARKED): checkout .git written in process instead of `git clone --local --shared --no-checkout`
- Code at a976850d (`git_cache.py:394-545` helpers; `:1211` call site): about +165 src lines including the differential. That is over the ~120 cap.
- Linux auth t50: 0.969 (18.36 -> 17.77 s; `clone` 100 -> 50; 45379854769).
- WINDOWS REGRESSION: auth t50 1.173 (39.32 -> 46.47 s; 45379914691); anonymous 1.123 (45380175587).
  - Git verbs show every first checkout failing on Windows and the package being re-fetched over the network: fetch +50, init +50, checkout 50 -> 100, round trips 453 -> 653.
  - Likely cause: the alternates line is written with backslashes (`os.path.abspath`), while git writes forward slashes. The differential compared with `os.path.normcase`, which hid it.
  - Fix to try: write the alternates path as git does (forward slashes) and compare it byte-exact in the differential. Then re-bench W (W3d S8 requires L, M and W).
- Cumulative a976850d vs 28f4e939 for the record: auth L 0.969 / W 1.096 (45379979669 / 45380035884); anonymous L 0.948 / W 1.123 (45380102000 / 45380175587).

## Update 13:1x CET: lever 4 KILLED (findings.md F6)
After the forward-slash alternates fix (518a7673, byte-exact differential 3b7d517c), W auth t50 is still 1.178 (45382610173). L 0.944 (45382652136). Every package is re-fetched on Windows after the in-process .git lands. The kill rule was W < 0.97. Ship set stays 2b + 3 (7b056a1b); landed in queue as #5352.
