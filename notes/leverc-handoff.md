# Lever C + 2a hand-off (PERF-BETS -> L5, git-path owner). v0.39.1 candidate.

Population: authenticated token-first and every non-github.com host, i.e. the git CLI path. Whole-repo deps only; subdir (sparse/promisor) deps stay on git.
Branch: perf-research/arch-gitpath-inproc-5351 @ f0e79d13, on #5351 head 33ec1c16 (#5330 + Windows follow-up + main incl. #5341). DEPENDS on #5351 (the W4 attribute gate); on #5330 alone it does not engage on Windows.
W3d rulings:
- 2a = SoL P1-P7.
- Lever C = w3d:ruling:lever-c, C1-C12.

## What it does
- src/apm_cli/cache/pack_objects.py (new, 389 lines):
  - PackObjects (:142): an idx v2 locator + pack reader (OFS/REF delta, B1's _inflate/_apply_delta caps). Every object is re-hashed by the caller (P1).
  - _objects_dirs (:99) refuses, so git answers:
    - a .git file, grafts, nested alternates, http-alternates, promisor packs or config, replace refs (dir or packed);
    - MIDX, SHA-256, 64-bit offsets, out-of-pack REF_DELTA, delta depth > 50.
- 2a: checkout_verify._verify (:482-500) tries _PackSource; any miss (_PackMiss) re-runs the whole package through `git cat-file --batch` (all-or-nothing, P-rule).
- C:
  - git_cache._create_checkout (:1146-1175): after `clone --local --shared --no-checkout` (C6, kept), materialize_tree (:239) writes the pinned tree. Not on sparse_paths or promisor_url (C4).
  - It then registers the B1 manifest (one-shot verify + links skip, C9: fresh landing only), logs "In-process checkout:", and lands via atomic_land (C8).
  - Gate: #5351's _network_env / _frozen_entries / _require_inert_attributes (W4). A filter, eol or encoding applies only through an attribute: with every outside attributes source inert and the commit's .gitattributes refused, filter.* definitions (e.g. git-lfs in the system config) are inert. An earlier filter.* refusal disengaged C on every runner with git-lfs.
  - Walk: commit -> tree, every object re-hashed; B1 _safe_name (.git*, .gitattributes, .gitmodules, NTFS/HFS names) and _plan caps; modes 100644/100755 only. Links 120000 and gitlinks 160000 -> git (C3+). Exec bit only from 100755 (C5+).
  - Writer (C8/C12): mkdir without exist_ok + B1 _write_file (O_EXCL|O_NOFOLLOW) under the fresh staged 0700 root.
  - _write_index (:351): a v2 index with real lstat data (C6), then a detached HEAD at the pin.
  - Any failure -> _discard_worktree (:334) leaves only .git -> today's `git checkout`.

## C6 reader list (git readers of a landed checkout)
- checkout_verify (objects + worktree, no index): same answer, by the 2a differential cell.
- git_cache._unmaterialized_paths `ls-files --deleted` (:273): promisor only, which C never serves.
- git_sparse ls-tree link plan: HEAD tree; skipped by the B1 manifest on the fresh landing, else reads the detached HEAD = pin.
- verify_checkout_sha: HEAD = pin.
- Cell test_the_pack_checkout_answers_every_git_reader_like_git_checkout asserts ls-files -s, status --porcelain, rev-parse HEAD, detached HEAD and diff == git checkout.
- L5 to confirm no other `git -C <checkout>` reader (grep showed none beyond these).

## Cells (tests/unit/cache/test_pack_checkout.py, tests/integration/test_pack_checkout_world.py)
- Parity of every git reader (C6).
- Planted link, commit .gitattributes, replace ref, flipped pack bytes, a swapped parent (file), and a parent swapped for a symlink (POSIX): each -> None and only .git left (C3+/C5/C8/C11).
- C11 differential: tree-10 offline, git path forced; pack checkout vs APM_NO_INPROCESS_CHECKOUT=1 give an identical lockfile, deployed bytes and receipts; engaged 10/10.
- C9: a second install re-verifies through git readers, same lockfile and tree.
- GREEN at f0e79d13, x3 each:
  - L [45405612058]: all 9 lever-C cells passed.
  - W [45405642375]: 8 passed; the symlink-parent cell is POSIX-only.
  - Plus tests/unit/cache and test_inprocess_fetch_world. The only failure is test_git_env.py::test_clone_streams_git_progress_to_reporter, the known harness-order artifact F&C confirmed is not ours.
- The tamper cell caught a real bug at db758066: zlib.error escaped the pack reader. It is fixed in f0e79d13 (any decode error refuses).

## Numbers (authenticated tree-50, paired CPU incl. children, 6 pairs)
- Prototype 419ee10a vs 33ec1c16:
  - W t50 27.56 -> 23.47 s = 0.862, t10 0.881 [45400831263];
  - L t50 15.73 -> 14.99 s = 0.973, t10 0.985 [45400799575].
  - Spawns 365 -> 165 (checkout 50, ls-tree 100, cat-file 50 -> 0) [profiles W 45400699120, L 45400752559].
- Conforming 41bfa795 (index + discard + W4-only gate; f0e79d13 differs only in zlib.error handling):
  - W t50 27.63 -> 23.17 s = 0.859, t10 0.884 [45404312946];
  - L t50 15.80 -> 15.09 s = 0.963, t10 0.983 [45404354323].
  - Spawns 365 -> 163-166.
- Real-history fixture, git path forced, anonymous (bench token invalid): W 0.974 [45401256127], L 0.993 [45401219674]. Only the one whole-repo dep engages; subdir deps are promisor -> git.

## Owed for the product PR (L5)
- Lever C cells: DONE (above).
- Rebase on #5351 once merged. Then the full W3d/QL H8, plus the 2a verdict differential + tamper cells for checkout_verify (P-rules).
- Decide whether to keep APM_NO_INPROCESS_CHECKOUT / APM_NO_INPROCESS_VERIFY as kill switches (as APM_NO_INPROCESS_FETCH).
- The complexity ratchet and the 120-line src budget: pack_objects.py is 389 lines, so expect to trim or share with B1's parser.
