# Single-pass integrate ceiling (PERF-SOL, CI only): KILL

Base: v0.39.0 stack e62f520f. #5382 conflicts with the stack in utils/realpath.py, so this is the plain stack.
Pop: anon. Paired 6 pairs, rtt 30, install-cold, OpenSSL world, 0 errors.

| variant (perf-research/arch-single-pass) | L t10 | L t50 | W t10 | W t50 | runs |
|---|---|---|---|---|---|
| A 306874b7: invocation memo of package-file bytes (Path.read_bytes/read_text, so classify, prepare_instruction, scan_file, load_frontmatter, render_copy_agent) + package-tree and exact-file hash memo | 0.999 | 1.002 | 0.998 | 0.999 | 45461174996 / 45461225856 |
| B d345643: A + _package_files walk memo + checkout_verify._read_regular memo + one journal fd per invocation | 0.989 | 0.993 | 0.989 | 0.987 | 45461604679 / 45461641952 |

Engagement proof (profiled B, 45462440650 vs base profile 45446802295):
- journal append_private 0.24 -> 0.04 s.
- _hash_package_tree / compute_exact_file_hash owners gone.
- _package_files_uncached (0.22 s) and _read_regular_uncached (0.12 s) still run: each path is walked and read ONCE already.
- First reads moved to research_memo.read_bytes/read_text (0.43 s).

Why it cannot reach 15% L:
- Within an invocation, reads are already one per path. The remaining duplication is the same bytes at 3 locations
  (cache checkout -> apm_modules copy -> deployed target), each read once.
- All read/hash/copy leaves together are ~0.35 s of the 5.88 s L profile (6%) and ~1.0 s of 16.5 s W. Even a perfect
  read-once-across-copies cannot clear 15% L, and the path memo measured <1%.
- The integrate phase cost is path containment (27%) plus per-file Python orchestration, not bytes.
- Profile sizing overstated again: the profile showed ~0.4 s L removed, the paired bench ~0.06 s.
No W3d brief and no E2 handoff (kill rule).
