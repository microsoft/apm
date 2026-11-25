## Description

A monorepo subdirectory dependency (`owner/repo/path#ref`) on the git path paid for a partial bare clone of every branch's full history, plus clone, sparse-checkout, checkout and lazy-fetch git children, to land a cone of a few KB. For example, `microsoft/apm` costs 22.5 MB, 114k objects and about 2 s of git CPU.

After #4970 admission and the same transport gate the in-process pinned fetch uses (#5330/#5351), such a dependency is now two protocol-v2 requests:

1. The pinned commit with all its trees (`deepen 1`, `filter blob:none`), only when the server advertises `fetch=` filter and shallow.
2. Exactly the blobs `git sparse-checkout --cone` would write. The response must be exactly that set.

Every object id is computed in process. The written tree is the same as git's sparse cone. The landed `.git` has the in-process fetch's shape plus `shallow`, the trees, the cone's blobs and git's `info/sparse-checkout` file, and no promisor remote.

The dependency goes to git, writing nothing, when:
- the cone holds a link, a gitlink, or an alias of `.gitattributes`;
- an in-cone `.gitattributes` could change any written file's bytes. This is evaluated per written file by `checkout_verify`'s attribute engine, also case-folded as `core.ignorecase` would match;
- the gate refuses (same conditions as the in-process fetch; on Windows that includes the `schannel` backend);
- any response or cap check fails.

The in-process sparse path is ON by default: no flag or config is needed to engage it. `APM_NO_INPROCESS_SPARSE=1` is only a kill switch that forces git. It keys the install verdict record, so a recorded verdict is never reused across its values.

Merge order: #5383 lands first; this PR rebases onto it (both touch `install/verdict_record.py`).

Measured at 9ae2a685 against its merge-base with main, bb4744f6 (which already includes #5353 and #5376), on real github.com: the bench's `real` fixture, 10 public packages, 9 of them monorepo subdirectory or file dependencies. Cold, 6 interleaved pairs, paired CPU including children. Population: public github.com, anonymous.

| OS / world | base CPU p50 | new CPU p50 | paired new/base | spawns | run |
|---|---|---|---|---|---|
| Linux | 6.21 s | 2.45 s | 0.450 | 78 -> 12 | 45513153542 |
| macOS | 6.99 s | 2.83 s | 0.386 | 86 -> 12 | 45513174573 |
| Windows (default Git for Windows, `schannel`) | 12.19 s | 7.00 s | 0.542 | 104 -> 45 | 45519097336 |

The Windows row runs the bench's real world with Git for Windows' system TLS config carried into it, as a default install has it (`schannel`). Before that harness fix, the isolated world hid `http.sslBackend=schannel`, so git ran OpenSSL with no named CA and neither main nor this PR fetched in process there (run 45513122272). A probe of the real `apm install` on a default runner shows the in-process hits (run 45518723251).

The control `tree-50` (no subdirectory deps), cold, 6 pairs, is neutral: Linux 1.010 (45513198595), Windows 0.995 (45513232286).

## Issue and approved scope

Issue: Refs #4939

Human scope-approval comment: none yet for this item. It was ordered by the PERF lead chain under #4939 (lever S). W3d ruled the conditions: `w3d:ruling:lever-s` S1-S12, `lever-s6prime` T1-T7 and `lever-s-t1prime` U1-U7.

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

Hosted-runner cells, x3 per OS:
- `tests/unit/cache` + `test_inprocess_fetch_world` + `test_inprocess_fetch_windows_world`: Linux 45447526059, Windows 45447565093, macOS 45447617664. All pass except `test_git_env.py::test_clone_streams_git_progress_to_reporter`, a known harness-order artifact that fails in isolation on main too and passes in the full PR run.
- `test_inprocess_sparse_live` passed x3 on every OS: Linux 45450785605 and macOS 45450839815 at 916c3f16, Windows 45449888782 at b7abc731 (the only later change is the engaged count, which Windows does not assert). Single-file dependencies take the file download path, not a sparse checkout.

- `tests/integration/test_inprocess_sparse_cone.py`:
  - cone and `info/sparse-checkout` byte-equal to git's for a directory, a file and a top-level cone;
  - a CRLF blob under `* text=auto eol=lf` is written as git writes it;
  - `eol=crlf`, `crlf`, `filter`, `ident`, `working-tree-encoding`, `[attr]`, quoted and unknown tokens refuse;
  - a pattern-aware `*.bat eol=crlf` with no `.bat` in the cone engages; a `.bat` or `RUN.BAT` in the cone refuses; a deeper override is evaluated by state;
  - an out-of-cone attributes file does not apply;
  - aliases of `.gitattributes` and a linked `.gitattributes` refuse;
  - a link in the cone refuses, and one outside the cone is ignored.
- `tests/integration/test_inprocess_sparse_attr_differential.py` (W3d U6): 40 seeded trees x 2 `core.ignorecase` modes; every path's attribute state equals `git check-attr -a`.
- `tests/integration/test_inprocess_sparse_live.py` (live): four real subdirectory dependencies on `microsoft/apm` and `github/awesome-copilot`, in process vs `APM_NO_INPROCESS_SPARSE=1`. Lockfile, deployed bytes and deploy-store receipts are identical, and every directory dependency engages on Linux and macOS.

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
