"""Wave 6 -- integration tests maximising coverage for outdated.py and view.py.

Strategy
--------
* Invoke CLI commands through ``click.testing.CliRunner`` so every line of the
  real Click-decorated functions is exercised.
* Only mock *external* I/O: ``GitHubPackageDownloader.list_remote_refs`` (the
  only outbound network call in these two modules) and auth-token env vars.
* Create realistic ``tmp_path`` fixtures with ``apm.yml``, ``apm.lock.yaml``
  and ``apm_modules/`` so the file-system code paths all fire.

Target modules
--------------
* ``src/apm_cli/commands/outdated.py``
* ``src/apm_cli/commands/view.py``
"""

from __future__ import annotations

from pathlib import Path
from typing import Never
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.dependency.types import GitReferenceType, RemoteRef

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LOCKFILE_TEMPLATE = """\
lockfile_version: "1"
generated_at: "2024-01-01T00:00:00+00:00"
dependencies:
{deps}"""

APM_YML_TEMPLATE = """\
name: test-project
version: 0.1.0
description: Test project
owner:
  name: test-org
dependencies:
  test-dep:
    version: ">=1.0.0"
    source: github:test-org/test-dep
"""


def _make_remote_ref(name: str, ref_type: GitReferenceType, sha: str) -> RemoteRef:
    """Create a production-shaped remote reference."""
    return RemoteRef(name=name, ref_type=ref_type, commit_sha=sha)


def _write_lockfile(tmp_path: Path, deps_yaml: str) -> None:
    """Write an apm.lock.yaml with the provided deps YAML block."""
    if deps_yaml.strip():
        content = (
            'lockfile_version: "1"\n'
            'generated_at: "2024-01-01T00:00:00+00:00"\n'
            "dependencies:\n"
            f"{deps_yaml}\n"
        )
    else:
        content = (
            'lockfile_version: "1"\ngenerated_at: "2024-01-01T00:00:00+00:00"\ndependencies: []\n'
        )
    (tmp_path / "apm.lock.yaml").write_text(content, encoding="utf-8")


def _write_apm_yml(tmp_path: Path, content: str = APM_YML_TEMPLATE) -> None:
    (tmp_path / "apm.yml").write_text(content, encoding="utf-8")


def _make_installed_package(
    tmp_path: Path,
    org: str = "test-org",
    repo: str = "test-repo",
    *,
    with_skill_md: bool = False,
    with_hooks: bool = False,
    with_workflows: bool = False,
) -> Path:
    """Create a package under apm_modules/ and return its path."""
    pkg_dir = tmp_path / "apm_modules" / org / repo
    pkg_dir.mkdir(parents=True, exist_ok=True)

    (pkg_dir / "apm.yml").write_text(
        f"name: {repo}\nversion: 1.0.0\ndescription: A test package\n"
        f"author: Test Author\nsource: github:{org}/{repo}\n",
        encoding="utf-8",
    )

    if with_skill_md:
        (pkg_dir / "SKILL.md").write_text("# Skill\nDoes stuff.\n", encoding="utf-8")

    if with_hooks:
        hooks_dir = pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "post-install.sh").write_text("#!/bin/sh\necho done\n", encoding="utf-8")

    if with_workflows:
        gh_dir = pkg_dir / ".github" / "workflows"
        gh_dir.mkdir(parents=True, exist_ok=True)
        (gh_dir / "test.yml").write_text("name: test\n", encoding="utf-8")

    return pkg_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# outdated -- no lockfile
# ---------------------------------------------------------------------------


class TestOutdatedNoLockfile:
    """Exercises the 'no lockfile found' error path."""

    def test_no_lockfile_exits_with_error(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        # No apm.lock.yaml

        result = runner.invoke(cli, ["outdated"])

        assert result.exit_code != 0 or "No lockfile" in result.output

    def test_global_no_lockfile(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--global flag resolves to ~/.apm/ scope; error message mentions ~/.apm/."""
        monkeypatch.chdir(tmp_path)

        # Patch get_apm_dir so the user scope points to tmp_path (no lockfile there)
        with patch(
            "apm_cli.core.scope.get_apm_dir",
            return_value=tmp_path,
        ):
            result = runner.invoke(cli, ["outdated", "--global"])

        assert result.exit_code != 0 or "lockfile" in result.output.lower()


# ---------------------------------------------------------------------------
# outdated -- empty lockfile
# ---------------------------------------------------------------------------


class TestOutdatedEmptyLockfile:
    """Exercises the 'no locked dependencies' early-exit path."""

    def test_empty_dependencies_list(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _write_lockfile(tmp_path, "")  # empty deps list

        result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0
        assert "No locked dependencies" in result.output


# ---------------------------------------------------------------------------
# outdated -- local and Artifactory deps are skipped
# ---------------------------------------------------------------------------


class TestOutdatedSkippedDeps:
    """Exercises the local/Artifactory skip paths."""

    def test_only_local_deps_skipped(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            "  - repo_url: ./local-pkg\n    source: local\n    local_path: ./local-pkg\n",
        )

        result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0
        assert "No remote dependencies" in result.output

    def test_only_artifactory_deps_skipped(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            "  - repo_url: test-org/test-repo\n    registry_prefix: artifactory/github\n",
        )

        result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0
        assert "No remote dependencies" in result.output


# ---------------------------------------------------------------------------
# outdated -- branch-pinned dep (up-to-date)
# ---------------------------------------------------------------------------


class TestOutdatedBranchUpToDate:
    """Branch dep whose locked SHA matches remote tip → up-to-date."""

    def test_branch_dep_up_to_date(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        sha = "aabbccdd11223344"
        _write_lockfile(
            tmp_path,
            f"  - repo_url: test-org/test-repo\n"
            f"    resolved_ref: main\n"
            f"    resolved_commit: {sha}\n",
        )

        branch_ref = _make_remote_ref("main", GitReferenceType.BRANCH, sha)

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            return_value=[branch_ref],
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0
        assert "up-to-date" in result.output.lower()


# ---------------------------------------------------------------------------
# outdated -- branch-pinned dep (outdated)
# ---------------------------------------------------------------------------


class TestOutdatedBranchOutdated:
    """Branch dep whose locked SHA differs from remote tip → outdated."""

    def test_branch_dep_outdated(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        old_sha = "0000000011111111"
        new_sha = "ffffffff22222222"
        _write_lockfile(
            tmp_path,
            f"  - repo_url: test-org/test-repo\n"
            f"    resolved_ref: main\n"
            f"    resolved_commit: {old_sha}\n",
        )

        branch_ref = _make_remote_ref("main", GitReferenceType.BRANCH, new_sha)

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            return_value=[branch_ref],
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0
        assert "outdated" in result.output.lower()

    def test_branch_dep_outdated_verbose(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--verbose flag is accepted without crash (branch path)."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            "  - repo_url: test-org/test-repo\n"
            "    resolved_ref: main\n"
            "    resolved_commit: 0000000011111111\n",
        )

        branch_ref = _make_remote_ref("main", GitReferenceType.BRANCH, "ffffffff22222222")

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            return_value=[branch_ref],
        ):
            result = runner.invoke(cli, ["outdated", "--verbose"])

        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# outdated -- tag-pinned dep (outdated)
# ---------------------------------------------------------------------------


class TestOutdatedTagOutdated:
    """Tag dep whose version is older than the newest remote tag → outdated."""

    def test_tag_dep_outdated(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            "  - repo_url: test-org/test-repo\n"
            "    resolved_ref: v1.0.0\n"
            "    resolved_commit: aabbccdd11223344\n",
        )

        tag_v2 = _make_remote_ref("v2.0.0", GitReferenceType.TAG, "deadbeef00000000")
        tag_v1 = _make_remote_ref("v1.0.0", GitReferenceType.TAG, "aabbccdd11223344")

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            return_value=[tag_v2, tag_v1],
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0
        assert "outdated" in result.output.lower()

    def test_tag_dep_outdated_verbose_extra_tags(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--verbose should include extra tag list for outdated tag deps."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            "  - repo_url: test-org/test-repo\n"
            "    resolved_ref: v1.0.0\n"
            "    resolved_commit: aabbccdd11223344\n",
        )

        refs = [
            _make_remote_ref("v2.0.0", GitReferenceType.TAG, "dead0000"),
            _make_remote_ref("v1.5.0", GitReferenceType.TAG, "beef0000"),
            _make_remote_ref("v1.0.0", GitReferenceType.TAG, "aabb0000"),
        ]

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            return_value=refs,
        ):
            result = runner.invoke(cli, ["outdated", "--verbose"])

        assert result.exit_code == 0
        assert "outdated" in result.output.lower()


# ---------------------------------------------------------------------------
# outdated -- tag-pinned dep (up-to-date)
# ---------------------------------------------------------------------------


class TestOutdatedTagUpToDate:
    """Tag dep already at latest → up-to-date."""

    def test_tag_dep_up_to_date(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            "  - repo_url: test-org/test-repo\n"
            "    resolved_ref: v2.0.0\n"
            "    resolved_commit: deadbeef00000000\n",
        )

        tag_v2 = _make_remote_ref("v2.0.0", GitReferenceType.TAG, "deadbeef00000000")

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            return_value=[tag_v2],
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0
        assert "up-to-date" in result.output.lower()


# ---------------------------------------------------------------------------
# outdated -- unknown / unreachable dep
# ---------------------------------------------------------------------------


class TestOutdatedUnknown:
    """Exercises the 'unknown' status paths."""

    def test_list_remote_refs_fails_gives_unknown(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            "  - repo_url: test-org/test-repo\n"
            "    resolved_ref: main\n"
            "    resolved_commit: aabbccdd\n",
        )

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            side_effect=RuntimeError("network error"),
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0
        assert "unknown" in result.output.lower()

    def test_no_remote_tip_found_gives_unknown(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Remote refs list is empty for branch dep → unknown."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            "  - repo_url: test-org/test-repo\n"
            "    resolved_ref: feature-branch\n"
            "    resolved_commit: aabbccdd\n",
        )

        # Only tag refs, no branch refs → no tip found
        tag_ref = _make_remote_ref("v1.0.0", GitReferenceType.TAG, "aabbccdd")

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            return_value=[tag_ref],
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0
        assert "unknown" in result.output.lower()

    def test_no_tag_refs_for_tag_dep_gives_unknown(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tag dep but remote has no tags → unknown."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            "  - repo_url: test-org/test-repo\n"
            "    resolved_ref: v1.0.0\n"
            "    resolved_commit: aabbccdd\n",
        )

        # Only branch refs, no tags
        branch_ref = _make_remote_ref("main", GitReferenceType.BRANCH, "aabbccdd")

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            return_value=[branch_ref],
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0
        assert "unknown" in result.output.lower()

    def test_dep_parse_fails_gives_unknown(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When DependencyReference.parse raises, the dep gets 'unknown' status."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        # Use a clearly bad repo_url that will fail parsing or is invalid
        _write_lockfile(
            tmp_path,
            "  - repo_url: test-org/test-repo\n"
            "    resolved_ref: main\n"
            "    resolved_commit: aabbccdd\n",
        )

        with patch(
            "apm_cli.models.dependency.reference.DependencyReference.parse",
            side_effect=ValueError("bad ref"),
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0
        assert "unknown" in result.output.lower()


# ---------------------------------------------------------------------------
# outdated -- sequential check (--parallel-checks 0)
# ---------------------------------------------------------------------------


class TestOutdatedSequential:
    """j=0 exercises the sequential fallback path in _check_deps_with_progress."""

    def test_sequential_check(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        sha = "aabbccdd11223344"
        _write_lockfile(
            tmp_path,
            f"  - repo_url: test-org/test-repo\n"
            f"    resolved_ref: main\n"
            f"    resolved_commit: {sha}\n",
        )

        branch_ref = _make_remote_ref("main", GitReferenceType.BRANCH, sha)

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            return_value=[branch_ref],
        ):
            result = runner.invoke(cli, ["outdated", "-j", "0"])

        assert result.exit_code == 0

    def test_sequential_check_dep_error_degrades(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unexpected error in a single dep check must not crash outdated.

        Regression for the sequential path, which previously let an exception
        from ``_check_one_dep`` (e.g. ``TypeError: '<' not supported between
        instances of 'MagicMock' and 'MagicMock'``) propagate and exit 1
        instead of degrading the dependency to an ``unknown`` row.
        """
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            "  - repo_url: test-org/test-repo\n"
            "    resolved_ref: main\n"
            "    resolved_commit: aabbccdd11223344\n",
        )

        with patch(
            "apm_cli.commands.outdated._check_one_dep",
            side_effect=TypeError("'<' not supported between MagicMock and MagicMock"),
        ):
            result = runner.invoke(cli, ["outdated", "-j", "0"])

        assert result.exit_code == 0
        assert "unknown" in result.output.lower()


class TestOutdatedParallel:
    """Multiple deps trigger the parallel code path."""

    def test_multiple_deps_parallel(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        sha = "aabbccdd11223344"
        _write_lockfile(
            tmp_path,
            f"  - repo_url: test-org/repo-one\n"
            f"    resolved_ref: main\n"
            f"    resolved_commit: {sha}\n"
            f"  - repo_url: test-org/repo-two\n"
            f"    resolved_ref: main\n"
            f"    resolved_commit: {sha}\n",
        )

        branch_ref = _make_remote_ref("main", GitReferenceType.BRANCH, sha)

        def _list_remote_refs(_downloader: object, _dep_ref: object) -> list[RemoteRef]:
            return [branch_ref]

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            new=_list_remote_refs,
        ):
            result = runner.invoke(cli, ["outdated", "-j", "2"])

        assert result.exit_code == 0

    def test_multiple_deps_some_outdated(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple deps where one is outdated exercises full table render."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        old_sha = "0000000011111111"
        new_sha = "ffffffff22222222"
        _write_lockfile(
            tmp_path,
            f"  - repo_url: test-org/repo-one\n"
            f"    resolved_ref: main\n"
            f"    resolved_commit: {old_sha}\n"
            f"  - repo_url: test-org/repo-two\n"
            f"    resolved_ref: main\n"
            f"    resolved_commit: {new_sha}\n",
        )

        def _list_remote_refs(_downloader: object, _dep_ref: object) -> list[RemoteRef]:
            return [_make_remote_ref("main", GitReferenceType.BRANCH, new_sha)]

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            new=_list_remote_refs,
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0
        # repo-one should be outdated, repo-two up-to-date
        assert "outdated" in result.output.lower()

    def test_multiple_deps_one_check_errors_degrades(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Parallel path: a raising dep check degrades to unknown, exit 0.

        Regression guard mirroring the sequential case so both paths handle an
        unexpected ``_check_one_dep`` failure identically.
        """
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        sha = "aabbccdd11223344"
        _write_lockfile(
            tmp_path,
            f"  - repo_url: test-org/repo-one\n"
            f"    resolved_ref: main\n"
            f"    resolved_commit: {sha}\n"
            f"  - repo_url: test-org/repo-two\n"
            f"    resolved_ref: main\n"
            f"    resolved_commit: {sha}\n",
        )

        def _raise_check_error(*_args: object, **_kwargs: object) -> Never:
            raise TypeError("'<' not supported between MagicMock and MagicMock")

        with patch(
            "apm_cli.commands.outdated._check_one_dep",
            new=_raise_check_error,
        ):
            result = runner.invoke(cli, ["outdated", "-j", "2"])

        assert result.exit_code == 0
        assert "unknown" in result.output.lower()


# ---------------------------------------------------------------------------
# outdated -- mixed local+remote deps
# ---------------------------------------------------------------------------


class TestOutdatedMixedDeps:
    """Local dep is skipped, remote dep is checked."""

    def test_local_dep_skipped_remote_checked(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        sha = "aabbccdd11223344"
        _write_lockfile(
            tmp_path,
            f"  - repo_url: ./local-pkg\n"
            f"    source: local\n"
            f"    local_path: ./local-pkg\n"
            f"  - repo_url: test-org/test-repo\n"
            f"    resolved_ref: main\n"
            f"    resolved_commit: {sha}\n",
        )

        branch_ref = _make_remote_ref("main", GitReferenceType.BRANCH, sha)

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            return_value=[branch_ref],
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0


# ===========================================================================
# view command tests
# ===========================================================================


class TestViewCommandNoModulesDir:
    """view command when apm_modules/ doesn't exist."""

    def test_no_apm_modules_exits(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        # No apm_modules/ directory

        result = runner.invoke(cli, ["view", "test-org/test-repo"])

        assert result.exit_code != 0
        assert "apm_modules" in result.output or "install" in result.output.lower()


class TestViewCommandPackageFound:
    """view command with an installed package."""

    def test_view_local_package(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _make_installed_package(tmp_path)

        result = runner.invoke(cli, ["view", "test-org/test-repo"])

        assert result.exit_code == 0
        # Should show package name or some metadata
        assert "test-repo" in result.output or "Package" in result.output

    def test_view_package_with_short_name(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Short (repo-only) name triggers fallback scan."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _make_installed_package(tmp_path)

        result = runner.invoke(cli, ["view", "test-repo"])

        assert result.exit_code == 0

    def test_view_package_with_lockfile_ref(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When lockfile has ref/commit, display_package_info shows them."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _make_installed_package(tmp_path)
        _write_lockfile(
            tmp_path,
            "  - repo_url: test-org/test-repo\n"
            "    resolved_ref: v1.2.3\n"
            "    resolved_commit: abcdef1234567890\n",
        )

        result = runner.invoke(cli, ["view", "test-org/test-repo"])

        assert result.exit_code == 0
        # Ref and commit should appear somewhere in output
        assert "v1.2.3" in result.output or "abcdef" in result.output

    def test_view_package_with_skill_md(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Package with SKILL.md alongside apm.yml (hybrid package)."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _make_installed_package(tmp_path, with_skill_md=True)

        result = runner.invoke(cli, ["view", "test-org/test-repo"])

        assert result.exit_code == 0

    def test_view_package_without_apm_yml(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Package directory without apm.yml (SKILL.md only)."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)

        # Create package with only SKILL.md (no apm.yml)
        pkg_dir = tmp_path / "apm_modules" / "test-org" / "skill-only"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "SKILL.md").write_text("# My Skill\nDoes things.\n", encoding="utf-8")

        result = runner.invoke(cli, ["view", "test-org/skill-only"])

        assert result.exit_code == 0

    def test_view_package_not_found(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-existent package → sys.exit(1) with helpful message."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _make_installed_package(tmp_path)

        result = runner.invoke(cli, ["view", "nonexistent/package"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "nonexistent" in result.output.lower()

    def test_view_path_traversal_rejected(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path traversal in package name is rejected."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        (tmp_path / "apm_modules").mkdir(exist_ok=True)

        result = runner.invoke(cli, ["view", "../../../etc/passwd"])

        assert result.exit_code != 0


class TestViewCommandVersionsField:
    """view <pkg> versions -- queries remote refs."""

    def test_versions_field_shows_refs(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)

        refs = [
            _make_remote_ref("v2.0.0", GitReferenceType.TAG, "deadbeef"),
            _make_remote_ref("main", GitReferenceType.BRANCH, "aabbccdd"),
        ]

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            return_value=refs,
        ):
            result = runner.invoke(cli, ["view", "test-org/test-repo", "versions"])

        assert result.exit_code == 0
        assert "v2.0.0" in result.output or "main" in result.output

    def test_versions_field_no_refs_found(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty remote refs list shows 'no versions found' message."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            return_value=[],
        ):
            result = runner.invoke(cli, ["view", "test-org/test-repo", "versions"])

        assert result.exit_code == 0
        assert "No versions" in result.output or "not found" in result.output.lower()

    def test_versions_field_network_error(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RuntimeError from list_remote_refs → exits with error."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
            side_effect=RuntimeError("network failure"),
        ):
            result = runner.invoke(cli, ["view", "test-org/test-repo", "versions"])

        assert result.exit_code != 0

    def test_unknown_field_rejected(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown field name → sys.exit(1) with helpful message."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)

        result = runner.invoke(cli, ["view", "test-org/test-repo", "badfield"])

        assert result.exit_code != 0
        assert "badfield" in result.output or "Unknown field" in result.output


class TestViewCommandAvailablePackages:
    """When package is not found, available packages are listed."""

    def test_available_packages_listed_on_not_found(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _make_installed_package(tmp_path, org="my-org", repo="my-pkg")

        result = runner.invoke(cli, ["view", "nonexistent"])

        assert result.exit_code != 0
        # Available packages should be listed
        assert "my-org" in result.output or "my-pkg" in result.output


class TestViewCommandWithHooks:
    """Packages with hooks show hook count."""

    def test_view_package_with_hooks(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)
        _make_installed_package(tmp_path, with_hooks=True)

        result = runner.invoke(cli, ["view", "test-org/test-repo"])

        assert result.exit_code == 0


class TestViewCommandGlobalScope:
    """--global flag resolves to user scope."""

    def test_view_global_no_modules_dir(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--global with empty user scope → 'no apm_modules/' error."""
        monkeypatch.chdir(tmp_path)

        # Point user scope to our tmp_path (no apm_modules/ there)
        with patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path):
            result = runner.invoke(cli, ["view", "test-org/test-repo", "--global"])

        assert result.exit_code != 0
        assert "apm_modules" in result.output or "install" in result.output.lower()

    def test_view_global_with_package(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--global with package installed in user scope."""
        monkeypatch.chdir(tmp_path)
        _make_installed_package(tmp_path)

        with patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path):
            result = runner.invoke(cli, ["view", "test-org/test-repo", "--global"])

        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# view -- display_versions with invalid reference
# ---------------------------------------------------------------------------


class TestViewVersionsInvalidRef:
    """display_versions with an unparseable reference exits with error."""

    def test_invalid_package_ref_for_versions(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        # An empty string or pure dot is not a valid DependencyReference
        with patch(
            "apm_cli.commands.view.DependencyReference.parse",
            side_effect=ValueError("bad ref"),
        ):
            result = runner.invoke(cli, ["view", "bad-ref", "versions"])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# view -- marketplace reference (NAME@MARKETPLACE pattern)
# ---------------------------------------------------------------------------


class TestViewMarketplaceRef:
    """view <plugin>@<marketplace> resolves via marketplace path."""

    def test_view_marketplace_ref_without_field(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NAME@MARKETPLACE without field calls _display_marketplace_plugin."""
        monkeypatch.chdir(tmp_path)

        mock_plugin = MagicMock()
        mock_plugin.name = "my-plugin"
        mock_plugin.version = "1.0.0"
        mock_plugin.description = "A test plugin"
        mock_plugin.source = {"type": "github", "repo": "test-org/plugin-repo", "ref": "main"}
        mock_plugin.tags = ["ai", "test"]

        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = mock_plugin

        mock_source = MagicMock()

        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("my-plugin", "test-marketplace", None),
            ),
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_or_cache",
                return_value=mock_manifest,
            ),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                return_value=("github:test-org/plugin-repo@main", MagicMock()),
            ),
        ):
            result = runner.invoke(cli, ["view", "my-plugin@test-marketplace"])

        assert result.exit_code == 0

    def test_view_marketplace_plugin_not_found(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plugin not in marketplace manifest → sys.exit(1)."""
        monkeypatch.chdir(tmp_path)

        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = None

        mock_source = MagicMock()

        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("missing-plugin", "test-marketplace", None),
            ),
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_or_cache",
                return_value=mock_manifest,
            ),
        ):
            result = runner.invoke(cli, ["view", "missing-plugin@test-marketplace"])

        assert result.exit_code != 0

    def test_view_marketplace_fetch_error(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MarketplaceFetchError from fetch_or_cache → sys.exit(1)."""
        monkeypatch.chdir(tmp_path)

        from apm_cli.marketplace.errors import MarketplaceFetchError

        mock_source = MagicMock()

        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("my-plugin", "test-marketplace", None),
            ),
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_or_cache",
                side_effect=MarketplaceFetchError("fetch failed"),
            ),
        ):
            result = runner.invoke(cli, ["view", "my-plugin@test-marketplace"])

        assert result.exit_code != 0

    def test_view_marketplace_registry_error(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_marketplace_by_name raises → sys.exit(1)."""
        monkeypatch.chdir(tmp_path)

        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("my-plugin", "bad-marketplace", None),
            ),
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                side_effect=Exception("not found"),
            ),
        ):
            result = runner.invoke(cli, ["view", "my-plugin@bad-marketplace"])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# display_package_info -- no context files, no workflows
# ---------------------------------------------------------------------------


class TestDisplayPackageInfoEdgeCases:
    """Cover edge cases in display_package_info rendering."""

    def test_package_no_description(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Package apm.yml without description field."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)

        pkg_dir = tmp_path / "apm_modules" / "test-org" / "nodesc-pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "apm.yml").write_text(
            "name: nodesc-pkg\nversion: 1.0.0\nauthor: Tester\nsource: github:test-org/nodesc-pkg\n",
            encoding="utf-8",
        )

        result = runner.invoke(cli, ["view", "test-org/nodesc-pkg"])

        assert result.exit_code == 0

    def test_package_no_apm_yml_no_skill_md(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Package with only some other file (resolved via SKILL.md path)."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)

        # Create package dir with just apm.yml (normal case, no context files)
        pkg_dir = tmp_path / "apm_modules" / "test-org" / "bare-pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "apm.yml").write_text(
            "name: bare-pkg\nversion: 2.0.0\n",
            encoding="utf-8",
        )

        result = runner.invoke(cli, ["view", "test-org/bare-pkg"])

        assert result.exit_code == 0
        assert "bare-pkg" in result.output or "2.0.0" in result.output


# ---------------------------------------------------------------------------
# _check_marketplace_ref path in outdated
# ---------------------------------------------------------------------------


class TestOutdatedMarketplaceDep:
    """Exercises _check_marketplace_ref in outdated for marketplace-sourced deps."""

    def test_marketplace_dep_up_to_date(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A marketplace dep whose installed ref matches marketplace ref → up-to-date."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)

        ref_sha = "abcdef1234567890"
        _write_lockfile(
            tmp_path,
            f"  - repo_url: test-org/test-plugin\n"
            f"    resolved_ref: {ref_sha}\n"
            f"    resolved_commit: {ref_sha}\n"
            f"    discovered_via: test-marketplace\n"
            f"    marketplace_plugin_name: test-plugin\n",
        )

        mock_plugin = MagicMock()
        mock_plugin.version = "1.0.0"
        mock_plugin.source = {"ref": ref_sha}

        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = mock_plugin

        mock_source = MagicMock()

        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_or_cache",
                return_value=mock_manifest,
            ),
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0
        assert "up-to-date" in result.output.lower()

    def test_marketplace_dep_outdated(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A marketplace dep whose installed ref differs from marketplace ref → outdated."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)

        installed_ref = "old_ref_sha_1234"
        latest_ref = "new_ref_sha_5678"

        _write_lockfile(
            tmp_path,
            f"  - repo_url: test-org/test-plugin\n"
            f"    resolved_ref: {installed_ref}\n"
            f"    resolved_commit: {installed_ref}\n"
            f"    discovered_via: test-marketplace\n"
            f"    marketplace_plugin_name: test-plugin\n",
        )

        mock_plugin = MagicMock()
        mock_plugin.version = "2.0.0"
        mock_plugin.source = {"ref": latest_ref}

        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = mock_manifest
        mock_manifest.find_plugin.return_value = mock_plugin

        mock_source = MagicMock()

        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_or_cache",
                return_value=mock_manifest,
            ),
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0
        assert "outdated" in result.output.lower()

    def test_marketplace_dep_no_plugin_found_fallback(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plugin not found in marketplace manifest → falls back to git check."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)

        sha = "aabbccdd11223344"
        _write_lockfile(
            tmp_path,
            f"  - repo_url: test-org/test-plugin\n"
            f"    resolved_ref: main\n"
            f"    resolved_commit: {sha}\n"
            f"    discovered_via: test-marketplace\n"
            f"    marketplace_plugin_name: test-plugin\n",
        )

        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = None  # plugin not found

        mock_source = MagicMock()
        branch_ref = _make_remote_ref("main", GitReferenceType.BRANCH, sha)

        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_or_cache",
                return_value=mock_manifest,
            ),
            patch(
                "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
                return_value=[branch_ref],
            ),
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0

    def test_marketplace_dep_string_source_fallback(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plugin with string source (relative path) falls back to git check."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)

        sha = "aabbccdd11223344"
        _write_lockfile(
            tmp_path,
            f"  - repo_url: test-org/test-plugin\n"
            f"    resolved_ref: main\n"
            f"    resolved_commit: {sha}\n"
            f"    discovered_via: test-marketplace\n"
            f"    marketplace_plugin_name: test-plugin\n",
        )

        mock_plugin = MagicMock()
        mock_plugin.version = "1.0.0"
        mock_plugin.source = "./relative/path"  # string source → fallback

        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = mock_plugin

        mock_source = MagicMock()
        branch_ref = _make_remote_ref("main", GitReferenceType.BRANCH, sha)

        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_or_cache",
                return_value=mock_manifest,
            ),
            patch(
                "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
                return_value=[branch_ref],
            ),
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0

    def test_marketplace_dep_fetch_error_fallback(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MarketplaceError during fetch_or_cache → falls back to git check."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)

        from apm_cli.marketplace.errors import MarketplaceError

        sha = "aabbccdd11223344"
        _write_lockfile(
            tmp_path,
            f"  - repo_url: test-org/test-plugin\n"
            f"    resolved_ref: main\n"
            f"    resolved_commit: {sha}\n"
            f"    discovered_via: test-marketplace\n"
            f"    marketplace_plugin_name: test-plugin\n",
        )

        mock_source = MagicMock()
        branch_ref = _make_remote_ref("main", GitReferenceType.BRANCH, sha)

        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_or_cache",
                side_effect=MarketplaceError("fetch failed"),
            ),
            patch(
                "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
                return_value=[branch_ref],
            ),
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0

    def test_marketplace_dep_registry_error_fallback(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MarketplaceError from get_marketplace_by_name → falls back to git check."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path)

        from apm_cli.marketplace.errors import MarketplaceError

        sha = "aabbccdd11223344"
        _write_lockfile(
            tmp_path,
            f"  - repo_url: test-org/test-plugin\n"
            f"    resolved_ref: main\n"
            f"    resolved_commit: {sha}\n"
            f"    discovered_via: test-marketplace\n"
            f"    marketplace_plugin_name: test-plugin\n",
        )

        branch_ref = _make_remote_ref("main", GitReferenceType.BRANCH, sha)

        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                side_effect=MarketplaceError("not found"),
            ),
            patch(
                "apm_cli.deps.github_downloader.GitHubPackageDownloader.list_remote_refs",
                return_value=[branch_ref],
            ),
        ):
            result = runner.invoke(cli, ["outdated"])

        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# view -- marketplace versions path
# ---------------------------------------------------------------------------


class TestViewVersionsMarketplace:
    """view <plugin>@<marketplace> versions → display_versions marketplace path."""

    def test_versions_marketplace_ref(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NAME@MARKETPLACE for versions field also exercises display_versions."""
        monkeypatch.chdir(tmp_path)

        mock_plugin = MagicMock()
        mock_plugin.name = "my-plugin"
        mock_plugin.version = "1.0.0"
        mock_plugin.description = "A test plugin"
        mock_plugin.source = {"type": "github", "repo": "test-org/plugin-repo", "ref": "main"}
        mock_plugin.tags = []

        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = mock_plugin

        mock_source = MagicMock()

        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_or_cache",
                return_value=mock_manifest,
            ),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                return_value=("github:test-org/plugin-repo@main", MagicMock()),
            ),
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("my-plugin", "test-marketplace", None),
            ),
        ):
            result = runner.invoke(cli, ["view", "my-plugin@test-marketplace", "versions"])

        assert result.exit_code == 0
