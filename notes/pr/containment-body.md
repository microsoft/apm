## Description

Containment checks resolve every path they guard: `resolve_for_containment`, `ensure_path_within`, `portable_relpath(s)` and `first_symlink_component`. A cold install pays tens of thousands of full symlink walks. Most of those paths sit under a few dozen directories.

Inside one command, a directory whose resolved form is its own exact text has no link anywhere on its path. `resolve_path` now records that proof in the command's invocation memo (`utils/realpath.py`, the one owner). A path directly under a proven directory then resolves with a single `lstat` of its leaf, and `first_symlink_component` checks only the leaf below a proven directory.

Anything in doubt takes today's full resolution:
- a link or any reparse-point leaf;
- a `..` component;
- a subclassed `Path`;
- any call outside a command.

User code mid-command (a lifecycle script) clears every proof through `forget_invocation_memos`. A tree from an earlier run is proven only by resolving it now, which sees its links. `portable_relpaths` resolves through `resolve_path` again, as `portable_relpath` does.

Proofs never outlive what they saw:
- R1: a directory is proven only when `lstat` sees it as a real directory now, never a missing tail.
- R2/R4: `realpath_win.release_held(path)` drops proofs at or below *path* on every platform. It runs before every APM rename, delete or link creation: the existing held-release seams, `write_seam.rename/rmdir/rmtree`, the deploy journal's removals and link restore, `journal_modes` and `file_ops.robust_copy_entry`. The closed-world rule `transport-platform-held-directory-release` now also counts `os.symlink` and `Path.symlink_to`.
- R3: nothing under a volatile component (`.git`, staging, `.inc.`, `.tmp`, backup, migration, refresh) is proven.

The W3d ruling `w3d:ruling:containment-once` (K1-K9) maps as follows:
- K1: proofs live in the invocation memo and drop at `forget_invocation_memos`.
- K2: proofs are made this command, by resolution.
- K3/K5: unchanged; no write or read path changes.
- K4: a new directory is proven at its first resolution, once it exists.
- K6: answers equal `Path.resolve()`, so relative paths and lockfiles are unchanged.
- K7: exact-text keys.
- K8: one owner (`utils/realpath.py`).
- K9: cells below.

W3d accepted at rows `w3d:5382:31f6b88d` (R1-R4) and `w3d:5382:de08a54d` (K off on win32). The Windows Paths reviewer acked the Windows seam.

K is not applied on win32: Windows keeps no proofs and every resolve takes #5374's exact forms (with K on top of #5374, Windows measured about +1% CPU).

This stacks on #5374 (Windows held directories). Measured at de08a54d against #5374's final head c109777b on offline tree-50, paired CPU including children. Population: all. Request, round-trip and connect counts are equal in every row.

| OS / world | journey | pairs | base -> new CPU p50 | paired new/base | run |
|---|---|---|---|---|---|
| macOS | cold | 6 | 8.00 -> 6.67 s | 0.874 | 45475587367 |
| macOS | warm | 6 | 8.95 -> 7.23 s | 0.798 | 45475587367 |
| Linux | cold | 6 | 6.61 -> 6.21 s | 0.975 | 45475637858 |
| Linux | warm | 6 | 6.25 -> 5.94 s | 0.983 | 45475637858 |
| Windows (bench world, OpenSSL) | cold | 9 | 15.28 -> 15.39 s | 1.008 | 45475540833 |
| Windows (bench world, OpenSSL) | warm | 9 | 18.47 -> 18.48 s | 0.988 | 45475540833 |

macOS -15%, Linux -1-2%, Windows unchanged (K not applied on win32). An earlier 15-pair Linux run on #5374 186e16d8 read 0.990 cold and 0.977 warm (45468238215).

## Issue and approved scope

Issue: Refs #4939

Human scope-approval comment: none yet for this item. It was ordered by the PERF lead chain under #4939 (lever K, containment once per root). W3d ruled `w3d:ruling:containment-once` K1-K9 OK in principle.

This does not complete #4939.

## Type of change

- [ ] Bug fix
- [x] New feature
- [ ] Documentation
- [ ] Maintenance / refactor

## Testing

- [ ] Tested locally
- [x] All existing tests pass
- [x] Added tests for new functionality (if applicable)

Hosted-runner cells at de08a54d, x3 per OS (`tests/unit/utils`, `tests/unit/install`, `tests/unit/integration`, `tests/integration/test_inprocess_fetch_world.py`, `tests/integration/test_architecture_held_directory_release.py`):
- Linux: 8648 passed [45475315488]. Windows: 8126 passed [45475430802]. macOS: 8644 passed [45475487105].
- The only other failure was the install LOC budget after the #5374 merge, fixed in 4737862e by a docstring rewrap (no budget raise).
- macOS also fails 2 cells in `test_deploy_snapshot.py` (read-only directory restore). These are pre-existing: the same two fail on main 7521c350 [45470681317].

New cells in `tests/unit/utils/test_realpath.py`:
- every plain and linked case resolves exactly as `Path.resolve()` inside one command, repeated so the proven form answers;
- 800 seeded paths match `os.path.realpath` inside one command;
- a link planted by user code mid-command is seen after `forget_invocation_memos`, and `ensure_path_within` refuses it;
- a link planted before the command is never proven;
- a leaf link under a proven directory is still found by `first_symlink_component`;
- nothing is proven outside a command;
- on Windows, a case-variant leaf resolves to the disk case;
- Windows keeps no proofs: every resolve takes the platform form;
- a missing directory is not proven before it exists, so a link created there later is found (R1);
- `release_held` drops the proofs below it (R2), and nothing under a volatile component is proven (R3);
- a proven directory that APM replaces with a link (via `write_seam.rmtree`, via `write_seam.rename`, or via a link copy) is refused (R4);
- held-release rule mutations: an unreleased `write_seam` rmtree, `os.symlink` and `symlink_to` each fail the rule.

## Changelog

- [x] One fragment per section, `changelog.d/<pr>.<section>.md`, holding
      one line: user-visible effect first, 25 words or fewer, ending with
      `(#PR)`; breaking changes only under upgrade notes; no Highlights; no
      new lines under `## [Unreleased]`. See `changelog.d/README.md` and
      `.github/instructions/changelog.instructions.md`.
- [ ] N/A -- internal-only change (CI, tests, refactor).

## Spec conformance (OpenAPM v0.1)

If this PR changes behaviour that an OpenAPM v0.1 `req-XXX` covers,
confirm the three-step ritual in the
[development guide](https://github.com/microsoft/apm/blob/main/docs/src/content/docs/contributing/development-guide.md#adding-or-changing-a-normative-requirement-openapm-v01):

- [ ] Spec edit: `docs/src/content/docs/specs/openapm-v0.1.md` updated
      (new/changed `<a id="req-XXX"></a>` anchor + prose + Appendix C
      row).
- [ ] Manifest edit: `docs/src/content/docs/specs/manifests/openapm-v0.1.requirements.yml`
      updated.
- [ ] Test edit: a `@pytest.mark.req("req-XXX")` test under
      `tests/spec_conformance/` added or extended.
- [ ] `CONFORMANCE.{md,json}` regenerated via
      `uv run --extra dev python -m tests.spec_conformance.gen_statement`
      and committed.
- [x] N/A -- this PR does not change OpenAPM-observable behaviour.
