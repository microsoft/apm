# Windows stat-cut handoff (for the Windows Paths engineer, to fold into ONE Windows lever per VP 17:38)

pop: Windows anonymous + authenticated cold; L/M sanity.
- Branch `perf-research/arch-stat-on-wpe`: your head 60fbc0af + #5352/#5353/#5354 = E f6245391; then stat-cut v2. The differential SHA is 28cae5d5; the measured SHA is 36dccfc2.
- The same code on main-based 81f096a0 is `perf-research/arch-stat-cut` (v2 b26c4514, differential 791fe82f).
- Src delta vs E: content_hash.py +80/-1, deploy_state.py +48/-1, deploy_store.py +7/-3. About 40 of those lines are the differential plus the kept original walks.

## What it changes (file:line at 36dccfc2)
1. `utils/content_hash.py:133-198` `_package_files`: one `os.scandir` walk. It uses DirEntry type data (`is_symlink`, `is_dir(follow_symlinks=False)`, `is_file`), which on Windows comes from the directory listing with no per-item syscall. It replaces `rglob` + `Path.is_symlink()` (lstat) + `Path.is_file()` (stat) per item.
   - Excluded dirs and skip_roots are pruned; the set is the same.
   - Directory links are not descended, as in 3.12 rglob.
   - W3d S1/S4: an entry whose find data carries FILE_ATTRIBUTE_REPARSE_POINT (junctions etc.) uses the original per-item Path probes.
   - The original walk stays as `_package_files_walk` (`:200`) for the differential.
2. `utils/content_hash.py:125-131, 89` `_read_regular_bytes`: W3d S3. The hashing read fstat-checks S_ISREG on the opened handle.
3. `install/deploy_state.py:62-100` `entry_state`: one `os.lstat` instead of is_symlink + is_file + is_dir + lexists + the mode lstat.
   - Links and reparse points go to `_entry_state_walk` (S4).
   - W3d S5: it hashes through the opened fd and requires the fd's (dev, ino) to equal the lstat's. ino == 0 or a mismatch falls back to the original sequence.
4. `cache/deploy_store.py:163-176` `_prepare`: one `_real_dir` lstat per directory instead of lexists + lstat. A missing directory is created without exist_ok (W3d S6) and then proven. The store root and bucket are still proven on every put.
5. `_STAT_DIFFERENTIAL` (`content_hash.py:122`, `deploy_state.py:58`): when True, both forms run and any difference raises.

## W3d (row w3d:ruling:statcut) S1-S7
- S1 junction/reparse falls back: DONE (item 1).
- S2 identical set and order: DONE (the differential).
- S3 fstat S_ISREG on the hash read: DONE (item 2).
- S4 links and reparse points use the original: DONE (items 1 and 3).
- S5 hash through the fd, dev/ino check: DONE (item 3).
- S6 mkdir without exist_ok: DONE (item 4).
- S7 CI differential cells on W and L (junction, link, read-only, replaced-between): TODO, for the PR.

## Evidence
Increment of v2 over your stack E (36dccfc2 vs f6245391, install-cold, 6 pairs):

| cell | t50 | t10 | run |
|---|---|---|---|
| W anon | 0.986 (17.63 -> 17.34 s) | 0.983 | 45439730141 |
| W auth | 1.010 (noise) | - | 45439813597 |
| L anon | 0.979 | - | 45439893328 |
| M anon | 0.982 | - | 45439964732 |

- Differential SHA 28cae5d5: 0 errors on W anon t10/t50 (45439534928) and on the W probe uninstall/prune/update/warm/no-op (45439632454).
- Stat-family calls on E + v2: 79,235 per W install, down from 111,342 at 81f096a0 (45440063436).
- The same v1 on main-based 81f096a0, without your form B: W anon 0.979, auth 0.980, L 0.980, M 0.972 (F13).

## What is left (stat-family calls per W cold t50 on E + v2)
- `_form_b_missing` lstat 8,485: yours (the missing-leaf proof).
- `path_security._is_link` 8,350: the containment walk. Your held dirs could answer it.
- `deploy_store._real_dir` 3,336: per-put store proof; kept by design.
- `find_files_by_glob` 3,000: the hardlink nlink check; DirEntry.stat has no nlink on Windows, so it is kept.
- check_collision 2,634; portable_relpaths 2,600 (IFP's VDT branch); find_plugin_json 2,450; skill_integrator 2,000; format_detection 1,750; `_write_git_dir` exists 1,574.
- Each is <= ~0.1 s on W. Together that is ~0.5 s of ~17 s.

## Verdict
On your stack the non-overlapping stat-cut is a ~1-2% lever: W anon 0.986, W auth within noise, L/M ~0.98. It is faithful and cheap, so fold it into your PR if line budget allows (rule 28: it stacks). Otherwise drop it. It does not move the W floor gap on its own.
