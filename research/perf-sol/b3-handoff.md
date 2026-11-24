# B3 keeper handoff: materialize from B1's verified in-memory blobs (for F&C)

Source: b69efdf0 (`perf-research/arch-tree-deploy`, PERF-BETS first slice, on B1 b0f7fde6). Owner of the measurement: PERF-SOL.

## What it changes (file:line in b69efdf0)
1. `src/apm_cli/cache/inprocess_fetch.py:551-583` -- `fetch_worktree` also returns `payload = [(posix path, verified bytes, executable)]`, the blob bytes B1 already SHA-1-checked against their ids.
2. `src/apm_cli/cache/inprocess_fetch.py:590-629` -- new `materialize_payload(target, payload)`: writes the payload into the empty `apm_modules/<pkg>` with the same `O_CREAT|O_EXCL|O_NOFOLLOW` writer B1 uses (mkdir per directory, no copy and no re-read).
3. `src/apm_cli/cache/checkout_verify.py:661-710` -- `_PAYLOADS` beside `_INPROCESS`; `register_inprocess_manifest(..., payload)`; `inprocess_payload(repo, sha)` returns the bytes ONLY when `inprocess_verified(repo, sha)` holds (this process fetched and verified that very directory, bound to its dev/ino). `forget_verdicts` clears both.
4. `src/apm_cli/cache/git_cache.py:515-519` -- passes the payload through to the register call.
5. `src/apm_cli/deps/github_downloader.py:1902-1917` -- the cached-checkout copy path: if `inprocess_payload(_cached)` exists -> `materialize_payload(target_path, payload)`; else the unchanged `robust_copy_entry` loop + `require_verified_copy` (cache hits from disk keep today's verify).
6. `src/apm_cli/utils/content_hash.py:6, 79-140, 145-147` -- seeded content hash. DROP IT from the keeper: it never hits on current main, because `cache_pin` writes `.apm-pin` into every package root after materialize and voids the per-directory proof (F1, profile 45360275351 / 45365280552). The 0.93x below comes from items 1-5.

## Size (rule 28: < ~150 src lines)
- b69efdf0 as is: 151 added / 14 removed src lines (5 files).
- Keeper = items 1-5 without item 6 and without the seed call in `materialize_payload`: ~80 added / ~14 removed (4 files). PASSES the bar.

## Rebase onto #5330
- At 59a386ca and at the current PR head 340a3f9f: ONE conflict, `src/apm_cli/cache/checkout_verify.py` `register_inprocess_manifest` signature/docstring (the PR reworded the docstring; b69 added the `payload` parameter). Trivial: keep the PR docstring and add the parameter. Every other hunk applies cleanly.

## W3d rank-4 conditions it meets
- Cond 1 (verify by construction only for trees THIS process fetched and verified via B1): `inprocess_payload` is gated on `inprocess_verified` (dev/ino-bound); any other source falls back to copy + `require_verified_copy`.
- Cond 2 (sha256 stays the persisted key): unchanged; content hash is still computed by the existing walker (item 6 dropped).
- Cond 4 (do not shrink apm_modules): the full payload is written (every blob of the commit's worktree, same set the copy loop wrote minus `.git`).
- Writer: same `O_EXCL|O_NOFOLLOW` flags as B1's checkout writer; target must be empty (`_Refuse` otherwise).
- Not claimed: cond 3 (discovery from tree entries) -- built and KILLED in F1 (no win once the W3d-conformant seal is paid).

## Evidence (install-cold, paired CPU p50, base b0f7fde6 -> b69efdf0)
| OS | t10 | t50 | run |
|---|---|---|---|
| Linux (6 pairs) | 0.984 | 0.932 (12.16 -> 11.43 s) | 45358796767 |
| macOS (6 pairs) | 0.954 (4.58 -> 3.81 s) | 0.934 (13.13 -> 12.59 s) | 45367057630 |
| Windows | 1.004 (B1 not engaged at b0f7fde6) | - | 45358824098; BETS is benching the Windows cell on arch-inproc-fetch-win |
Counts unchanged (101 requests, ~232-240 spawns at t50). Judge again at the combined head with #5330 (Atlas rows 12, 20).
