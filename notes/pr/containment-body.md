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
- R2/R4: `realpath_win.release_held(path)` drops every proof on every platform (a prefix drop would miss link and case aliases). It runs before every APM rename, delete or link creation: the existing held-release seams, `write_seam.rename/rmdir/rmtree`, the deploy journal's removals and link restore, `journal_modes` and `file_ops.robust_copy_entry`. The closed-world rule `transport-platform-held-directory-release` now also counts `os.symlink` and `Path.symlink_to`.
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

W3d accepted at rows `w3d:5382:31f6b88d` (R1-R4) and `w3d:5382:de08a54d` (K off on win32); `w3d:5382:0bca2c4b` (K only where no kernel form applies) and `w3d:5382:c8eb343f` (clear-all invalidation, no `..` proofs). The Windows Paths reviewer acked the Windows seam.

K applies only where no fast platform form does (macOS and other POSIX). Linux keeps no proofs: the kernel forms (O_PATH, and #5372's `openat2` with `RESOLVE_NO_SYMLINKS`) already prove a link-free path in one syscall. Windows keeps no proofs: every resolve takes #5374's exact forms. On both, K measured no faster, so they run exactly main's and #5374's code.

This stacks on #5374 (Windows held directories), measured against its head 427c0876. Offline tree-50, paired CPU including children. Population: all.

| OS | journey | pairs | paired new/base | run |
|---|---|---|---|---|
| macOS | cold | 6 | 0.900 | 45517564444 |
| macOS | warm | 6 | 0.890 | 45517564444 |
| Linux | cold / warm | 6 | 1.010 / 0.995 (same resolve code as base) | 45515025450 |
| Windows | cold | 15 | 0.993 (same resolve code as base) | 45516164352 |
| Windows | warm | 9 | 0.987 | 45514979849 |
| Windows A/A (427c0876 vs itself) | cold | 9 | 1.007 (noise floor) | 45516140729 |

Request, round-trip and connect counts are equal in every row.

macOS: cold -10%, warm -11% CPU (measured at c8eb343f, where any APM mutation drops every proof; before that change macOS read 0.827 / 0.836, run 45514998880). Linux and Windows unchanged (K not applied there).

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

Hosted-runner cells at 0bca2c4b, x3 per OS (`tests/unit/utils`, `tests/unit/install`, `tests/unit/integration`, `tests/integration/test_inprocess_fetch_world.py`, `tests/integration/test_architecture_held_directory_release.py`):
- Linux: 8704 passed [45514905028]. Windows: 8165 passed [45514956695]. macOS: 8695 passed [45514929769].
- macOS also fails 2 cells in `test_deploy_snapshot.py` (read-only directory restore). These are pre-existing: the same two fail on main 7521c350 [45470681317].

New cells in `tests/unit/utils/test_realpath.py`:
- every plain and linked case resolves exactly as `Path.resolve()` inside one command, repeated so the proven form answers;
- 800 seeded paths match `os.path.realpath` inside one command;
- a link planted by user code mid-command is seen after `forget_invocation_memos`, and `ensure_path_within` refuses it;
- a link planted before the command is never proven;
- a leaf link under a proven directory is still found by `first_symlink_component`;
- nothing is proven outside a command;
- on Windows, a case-variant leaf resolves to the disk case;
- Linux (kernel forms) and Windows (exact forms) keep no proofs;
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
