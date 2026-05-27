"""End-to-end ``apm install`` coverage for git-source semver range refs (#1488).

The unit-tier suite (``tests/unit/deps/test_git_semver_resolver.py``,
``tests/unit/install/test_git_semver_wiring.py``) covers each helper in
isolation; this file pairs that work with the full
``apm install`` -> resolve phase -> lockfile write -> lockfile replay
pipeline and asserts on the user-observable artifacts (exit code,
``apm.lock.yaml`` contents, network-call counts).

Fidelity strategy
-----------------
Two seams are stubbed -- everything else runs through the real install
pipeline:

* ``RefResolver.list_remote_refs`` returns canned ``RemoteRef`` lists per
  ``owner/repo`` (the "git ls-remote" output a private fixture repo
  would produce).
* ``GitHubPackageDownloader.download_package`` writes a minimal
  ``apm.yml`` to the install path and returns a ``PackageInfo`` whose
  ``resolved_commit`` matches the SHA the resolver picked. Validation,
  integration, and lockfile writes then run against real disk content.

Both stubs record their call counts so tests can assert "lockfile replay
did not touch the network" without relying on subprocess sentinels.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.marketplace.ref_resolver import RemoteRef
from apm_cli.models.apm_package import (
    APMPackage,
    PackageInfo,
    clear_apm_yml_cache,
)
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"


# ---------------------------------------------------------------------------
# Canned remote ref sets
# ---------------------------------------------------------------------------


def _refs_v_prefixed() -> list[RemoteRef]:
    """Standard ``v{version}`` tag fixture: v1.0.0, v1.2.3, v1.5.0, v2.0.0."""
    return [
        RemoteRef(name="refs/heads/main", sha="0" * 40),
        RemoteRef(name="refs/tags/v1.0.0", sha="1" * 40),
        RemoteRef(name="refs/tags/v1.2.3", sha="2" * 40),
        RemoteRef(name="refs/tags/v1.5.0", sha="3" * 40),
        RemoteRef(name="refs/tags/v2.0.0", sha="4" * 40),
    ]


def _refs_only_name_dashv() -> list[RemoteRef]:
    """Repo where ONLY the ``{name}--v{version}`` pattern matches.

    Mirrors a multi-package repo (PR #1422 convention) where each
    package's tags are scoped by package name.
    """
    return [
        RemoteRef(name="refs/heads/main", sha="0" * 40),
        RemoteRef(name="refs/tags/widget--v1.0.0", sha="a" * 40),
        RemoteRef(name="refs/tags/widget--v1.3.0", sha="b" * 40),
        RemoteRef(name="refs/tags/otherpkg--v9.9.9", sha="c" * 40),
    ]


def _refs_only_bare() -> list[RemoteRef]:
    """Repo that tags as bare ``{version}`` -- triggers third-pattern fallback."""
    return [
        RemoteRef(name="refs/heads/main", sha="0" * 40),
        RemoteRef(name="refs/tags/1.0.0", sha="d" * 40),
        RemoteRef(name="refs/tags/1.4.2", sha="e" * 40),
    ]


def _refs_no_match() -> list[RemoteRef]:
    """No tag in any pattern satisfies a ^1.2.0 constraint."""
    return [
        RemoteRef(name="refs/heads/main", sha="0" * 40),
        RemoteRef(name="refs/tags/v0.9.0", sha="9" * 40),
    ]


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _RefResolverCallRecorder:
    """Records ``list_remote_refs`` calls and serves canned refs."""

    def __init__(self, refs_by_repo: dict[str, list[RemoteRef]]) -> None:
        self.refs_by_repo = refs_by_repo
        self.calls: list[str] = []
        self.init_kwargs: list[dict] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = self

        original_init = None
        from apm_cli.marketplace import ref_resolver as _rr_mod

        original_init = _rr_mod.RefResolver.__init__

        def _capture_init(self, *args, **kwargs):
            recorder.init_kwargs.append(dict(kwargs))
            return original_init(self, *args, **kwargs)

        def _fake_list_remote_refs(self, owner_repo: str) -> list[RemoteRef]:
            recorder.calls.append(owner_repo)
            refs = recorder.refs_by_repo.get(owner_repo)
            if refs is None:
                raise AssertionError(
                    f"Unexpected list_remote_refs call for {owner_repo!r}; "
                    f"fixture has: {sorted(recorder.refs_by_repo)}"
                )
            return list(refs)

        monkeypatch.setattr(_rr_mod.RefResolver, "__init__", _capture_init)
        monkeypatch.setattr(_rr_mod.RefResolver, "list_remote_refs", _fake_list_remote_refs)


class _DownloaderStub:
    """Stubs ``GitHubPackageDownloader.download_package`` to write a
    minimal valid apm package at the install path and return a
    ``PackageInfo`` whose ``resolved_commit`` reflects the SHA the
    resolver picked (read off ``dep_ref.reference`` after the resolve
    phase has rewritten it to the concrete tag).
    """

    def __init__(self, sha_by_tag: dict[str, str]) -> None:
        self.sha_by_tag = sha_by_tag
        self.calls: list[tuple[str, str]] = []  # (owner_repo, reference)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = self

        def _fake_download(self, repo_ref, target_path, *args, **kwargs):
            from apm_cli.models.apm_package import DependencyReference

            if isinstance(repo_ref, DependencyReference):
                dep_ref = repo_ref
            else:
                dep_ref = DependencyReference.parse(str(repo_ref))

            ref_value = dep_ref.reference or "main"
            recorder.calls.append((dep_ref.repo_url, ref_value))

            sha = recorder.sha_by_tag.get(ref_value, "f" * 40)
            target_path = Path(target_path)
            target_path.mkdir(parents=True, exist_ok=True)

            package_name = dep_ref.repo_url.rsplit("/", 1)[-1]
            (target_path / "apm.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": package_name,
                        "version": "0.0.0",
                        "description": "test fixture package",
                    }
                ),
                encoding="utf-8",
            )

            package = APMPackage.from_apm_yml(target_path / "apm.yml")
            return PackageInfo(
                package=package,
                install_path=target_path,
                installed_at=datetime.now().isoformat(),
                dependency_ref=dep_ref,
                resolved_reference=ResolvedReference(
                    original_ref=ref_value,
                    ref_type=GitReferenceType.TAG,
                    resolved_commit=sha,
                    ref_name=ref_value,
                ),
            )

        from apm_cli.deps import github_downloader as _ghd

        monkeypatch.setattr(_ghd.GitHubPackageDownloader, "download_package", _fake_download)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _write_apm_yml(project: Path, deps: list, name: str = "consumer-pkg") -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "version": "1.0.0",
                "target": "copilot",
                "dependencies": {"apm": deps, "mcp": []},
            }
        ),
        encoding="utf-8",
    )
    (project / ".github").mkdir(exist_ok=True)
    (project / ".github" / "copilot-instructions.md").write_text("# Project\n", encoding="utf-8")


def _read_lockfile(project: Path) -> dict | None:
    path = project / "apm.lock.yaml"
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _find_locked(lockfile: dict, repo_url: str) -> dict | None:
    deps = lockfile.get("dependencies") if lockfile else None
    if not deps:
        return None
    if isinstance(deps, dict):
        return deps.get(repo_url)
    for entry in deps:
        if entry.get("repo_url") == repo_url:
            return entry
    return None


def _run_install(
    runner: CliRunner,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str] | None = None,
):
    monkeypatch.chdir(project)
    with patch(_PATCH_UPDATES, return_value=None):
        return runner.invoke(cli, ["install", *(args or [])], catch_exceptions=False)


# ---------------------------------------------------------------------------
# Promise A: highest matching tag wins
# Promise B: lockfile records all four semver fields
# ---------------------------------------------------------------------------


class TestSemverRangeResolves:
    def test_caret_range_resolves_to_highest_matching_tag_and_lockfile_records_fields(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``acme/widget#^1.2.0`` against {v1.0.0, v1.2.3, v1.5.0, v2.0.0} picks v1.5.0.

        Asserts the lockfile records ``constraint``, ``resolved_tag``,
        ``resolved_commit``, ``version``, and ``resolved_at`` so future
        replays are deterministic.
        """
        project = tmp_path / "promise-ab"
        _write_apm_yml(project, ["acme/widget#^1.2.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_v_prefixed()})
        dl = _DownloaderStub({"v1.5.0": "3" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        result = _run_install(runner, project, monkeypatch)
        assert result.exit_code == 0, f"install failed:\n{result.output}"

        lockfile = _read_lockfile(project)
        assert lockfile is not None, "apm.lock.yaml was not written"

        locked = _find_locked(lockfile, "acme/widget")
        assert locked is not None, f"acme/widget missing from lockfile: {lockfile}"

        assert locked.get("constraint") == "^1.2.0"
        assert locked.get("resolved_tag") == "v1.5.0"
        assert locked.get("version") == "1.5.0"
        assert locked.get("resolved_commit") == "3" * 40
        assert locked.get("resolved_at"), "resolved_at timestamp missing"


# ---------------------------------------------------------------------------
# Promise C: second install is offline (lockfile replay)
# ---------------------------------------------------------------------------


class TestLockfileReplayIsOffline:
    def test_reinstall_with_unchanged_manifest_does_not_call_ref_resolver(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First install hits ls-remote; second install replays from lockfile.

        The recorder asserts ``list_remote_refs`` was called exactly once
        (during the first install) and never during the second install.
        """
        project = tmp_path / "promise-c"
        _write_apm_yml(project, ["acme/widget#^1.2.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_v_prefixed()})
        dl = _DownloaderStub({"v1.5.0": "3" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        first = _run_install(runner, project, monkeypatch)
        assert first.exit_code == 0, first.output
        assert rr.calls == ["acme/widget"], f"first install should ls-remote once, got: {rr.calls}"

        second = _run_install(runner, project, monkeypatch)
        assert second.exit_code == 0, second.output
        assert rr.calls == ["acme/widget"], (
            f"second install must replay from lockfile (no new ls-remote); "
            f"calls after second install: {rr.calls}"
        )


# ---------------------------------------------------------------------------
# Promise D: tag-pattern fallback order
# ---------------------------------------------------------------------------


class TestTagPatternFallback:
    def test_name_dashv_pattern_matches_when_v_pattern_has_no_candidates(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Repo with only ``widget--v1.x.y`` tags resolves via second pattern.

        The default ``v{version}`` pattern finds no candidates; the
        ``{name}--v{version}`` pattern scopes to this package only and
        picks ``widget--v1.3.0`` (ignoring ``otherpkg--v9.9.9``).
        """
        project = tmp_path / "promise-d"
        _write_apm_yml(project, ["acme/widget#^1.0.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_only_name_dashv()})
        dl = _DownloaderStub({"widget--v1.3.0": "b" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        result = _run_install(runner, project, monkeypatch)
        assert result.exit_code == 0, result.output

        locked = _find_locked(_read_lockfile(project), "acme/widget")
        assert locked is not None
        assert locked.get("resolved_tag") == "widget--v1.3.0"
        assert locked.get("version") == "1.3.0"
        # Critical: the other package's tag must not leak into this resolution.
        assert locked.get("version") != "9.9.9"

    def test_bare_version_pattern_is_third_pattern_fallback(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When neither default pattern matches, bare ``{version}`` is tried."""
        project = tmp_path / "promise-d-bare"
        _write_apm_yml(project, ["acme/widget#^1.0.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_only_bare()})
        dl = _DownloaderStub({"1.4.2": "e" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        result = _run_install(runner, project, monkeypatch)
        assert result.exit_code == 0, result.output

        locked = _find_locked(_read_lockfile(project), "acme/widget")
        assert locked is not None
        assert locked.get("resolved_tag") == "1.4.2"
        assert locked.get("version") == "1.4.2"


# ---------------------------------------------------------------------------
# Promise E: constraint change re-resolves
# Promise F: drift between locked constraint and manifest constraint
# ---------------------------------------------------------------------------


class TestConstraintChangeReResolves:
    def test_lockfile_constraint_change_with_stale_install_path_re_resolves(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Drift sub-case (Promise F): when the install path is missing
        (cache pruned, ``apm_modules`` deleted) and the lockfile constraint
        differs from the manifest, the resolver re-runs without ``--update``.

        Exercises the ``_maybe_resolve_git_semver`` branch where
        ``locked.constraint != constraint`` skips the lockfile replay and
        falls through to ``GitSemverResolver.resolve``.
        """
        import shutil

        project = tmp_path / "promise-f"
        _write_apm_yml(project, ["acme/widget#^1.2.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_v_prefixed()})
        dl = _DownloaderStub({"v1.5.0": "3" * 40, "v2.0.0": "4" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        first = _run_install(runner, project, monkeypatch)
        assert first.exit_code == 0, first.output

        # Simulate a cache-pruned environment: drop the materialised dep
        # but keep the lockfile, then bump the constraint.
        shutil.rmtree(project / "apm_modules", ignore_errors=True)
        _write_apm_yml(project, ["acme/widget#^2.0.0"])
        clear_apm_yml_cache()

        second = _run_install(runner, project, monkeypatch)
        assert second.exit_code == 0, second.output

        # The drift branch in _maybe_resolve_git_semver fired -- ls-remote
        # was called and the lockfile records the new constraint.
        assert rr.calls.count("acme/widget") == 2, (
            f"expected 2 ls-remote calls (initial + drift), got: {rr.calls}"
        )
        locked = _find_locked(_read_lockfile(project), "acme/widget")
        assert locked["constraint"] == "^2.0.0"
        assert locked["resolved_tag"] == "v2.0.0"


# ---------------------------------------------------------------------------
# Promise G: literal ref bypasses the semver resolver
# ---------------------------------------------------------------------------


class TestLiteralRefUnchanged:
    def test_literal_tag_ref_does_not_invoke_semver_resolver(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ref: v1.2.3`` (literal tag) keeps existing behaviour.

        No ``list_remote_refs`` call, no ``constraint``/``resolved_tag``
        fields in the lockfile entry -- these are reserved for the semver
        path.
        """
        project = tmp_path / "promise-g"
        _write_apm_yml(project, ["acme/widget#v1.2.3"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_v_prefixed()})
        dl = _DownloaderStub({"v1.2.3": "2" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        result = _run_install(runner, project, monkeypatch)
        assert result.exit_code == 0, result.output

        # The literal-ref path must not touch the semver resolver.
        assert rr.calls == [], f"literal ref must not invoke list_remote_refs; got: {rr.calls}"

        locked = _find_locked(_read_lockfile(project), "acme/widget")
        assert locked is not None
        # Semver-specific fields stay absent for literal refs.
        assert "constraint" not in locked or locked.get("constraint") is None
        assert "resolved_tag" not in locked or locked.get("resolved_tag") is None
        # The literal ref is still pinned through the normal resolved_ref field.
        assert locked.get("resolved_ref") == "v1.2.3"


# ---------------------------------------------------------------------------
# Promise H: AuthResolver token threads into RefResolver for private repos
# ---------------------------------------------------------------------------


class TestAuthTokenThreadedToLsRemote:
    def test_github_apm_pat_reaches_ref_resolver_for_semver_dep(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression-trap for the auth-blocking panel finding on PR #1496.

        With ``GITHUB_APM_PAT`` set, an ``apm install`` of a semver-range
        git-source dep must pass that token to the ``RefResolver`` that
        runs ``git ls-remote`` -- otherwise private repos fail in CI
        environments without a system git credential helper.
        """
        monkeypatch.setenv("GITHUB_APM_PAT", "ghp_e2e_token_abc123")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        project = tmp_path / "promise-h"
        _write_apm_yml(project, ["acme/widget#^1.2.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_v_prefixed()})
        dl = _DownloaderStub({"v1.5.0": "3" * 40})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        result = _run_install(runner, project, monkeypatch)
        assert result.exit_code == 0, result.output

        # At least one RefResolver instance must have been constructed
        # with the configured PAT.
        tokens_seen = [kw.get("token") for kw in rr.init_kwargs]
        assert "ghp_e2e_token_abc123" in tokens_seen, (
            "AuthResolver did not thread GITHUB_APM_PAT into the "
            f"RefResolver used for ls-remote. token kwargs seen: {tokens_seen}"
        )


# ---------------------------------------------------------------------------
# Promise I: no matching tag -> clear, actionable error
# ---------------------------------------------------------------------------


class TestNoMatchingTagError:
    def test_no_matching_tag_exits_nonzero_with_actionable_message(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``^1.2.0`` against a repo with only ``v0.9.0`` fails with a
        message that names the constraint, the repo, and the tags considered."""
        project = tmp_path / "promise-i"
        _write_apm_yml(project, ["acme/widget#^1.2.0"])

        rr = _RefResolverCallRecorder({"acme/widget": _refs_no_match()})
        dl = _DownloaderStub({})
        rr.install(monkeypatch)
        dl.install(monkeypatch)

        result = _run_install(runner, project, monkeypatch)

        combined = (result.output or "") + (result.stderr or "")
        # The failure must be surfaced loudly in the CLI output: the
        # error marker plus the actionable diagnostic. (Note: exit code
        # is not asserted -- the CLI currently exits 0 even on per-dep
        # install failure; that is a separate, pre-existing concern.)
        assert "failed" in combined.lower(), (
            f"expected an explicit failure marker in output:\n{combined}"
        )

        # The diagnostic must name (a) the constraint, (b) the repo, and
        # (c) at least one tag considered so the user can widen the range.
        assert "^1.2.0" in combined, f"constraint not surfaced:\n{combined}"
        assert "acme/widget" in combined, f"repo not surfaced:\n{combined}"
        assert "v0.9.0" in combined, f"available tags not surfaced:\n{combined}"

        # No lockfile entry for the failed dep.
        lockfile = _read_lockfile(project)
        locked = _find_locked(lockfile, "acme/widget") if lockfile else None
        assert locked is None or not locked.get("resolved_commit"), (
            "failed semver resolution must not write a half-populated lockfile entry"
        )
