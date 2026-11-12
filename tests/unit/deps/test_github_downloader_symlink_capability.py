"""The symlink capability diagnostic is final, not a cue to retry another path (#3457)."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import DependencyReference, GitReferenceType, ResolvedReference
from apm_cli.utils.git_env import GitSymlinkCapabilityError

pytestmark = [pytest.mark.unit]

_SHA = "a" * 40


@pytest.fixture
def downloader() -> GitHubPackageDownloader:
    auth = MagicMock()
    host_info = MagicMock()
    host_info.kind = "github"
    auth.classify_host.return_value = host_info
    ctx = MagicMock()
    ctx.token = None
    ctx.auth_scheme = "basic"
    ctx.git_env = {}
    auth.resolve.return_value = ctx
    auth.resolve_for_dep.return_value = ctx
    auth.uses_public_github_anonymous_first.return_value = False
    auth._token_manager = MagicMock()
    auth._token_manager.get_token_for_purpose.return_value = None
    return GitHubPackageDownloader(auth_resolver=auth)


def _resolved() -> ResolvedReference:
    return ResolvedReference(
        original_ref="main",
        ref_name="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=_SHA,
    )


def _no_artifactory(stack: ExitStack, downloader: GitHubPackageDownloader) -> None:
    for name, value in (
        ("_is_artifactory_only", False),
        ("_parse_artifactory_base_url", None),
        ("_should_use_artifactory_proxy", False),
        ("resolve_git_reference", _resolved()),
        ("_git_env_dict", {}),
    ):
        stack.enter_context(patch.object(downloader, name, return_value=value))


def test_whole_repo_cache_capability_error_is_not_retried_by_clone(
    downloader: GitHubPackageDownloader, tmp_path: Path
) -> None:
    dep = DependencyReference(repo_url="owner/repo", reference="main")
    cache = MagicMock()
    cache.get_checkout.side_effect = GitSymlinkCapabilityError("owner/repo")
    downloader.persistent_git_cache = cache

    with ExitStack() as stack:
        _no_artifactory(stack, downloader)
        clone = stack.enter_context(
            patch.object(
                downloader,
                "_clone_with_fallback",
                side_effect=[GitSymlinkCapabilityError("owner/repo"), MagicMock()],
            )
        )
        with pytest.raises(GitSymlinkCapabilityError, match="owner/repo"):
            downloader.download_package(dep, tmp_path / "out")

    clone.assert_not_called()


def test_subdirectory_cache_capability_error_is_not_retried_by_clone(
    downloader: GitHubPackageDownloader, tmp_path: Path
) -> None:
    dep = DependencyReference(
        repo_url="owner/repo",
        reference="main",
        virtual_path="skills/demo",
        is_virtual=True,
    )
    cache = MagicMock()
    cache.get_checkout.side_effect = GitSymlinkCapabilityError("owner/repo")
    downloader.persistent_git_cache = cache

    with ExitStack() as stack:
        _no_artifactory(stack, downloader)
        sparse = stack.enter_context(patch.object(downloader, "_try_sparse_checkout"))
        clone = stack.enter_context(
            patch.object(
                downloader,
                "_clone_with_fallback",
                side_effect=[GitSymlinkCapabilityError("owner/repo"), MagicMock()],
            )
        )
        with pytest.raises(GitSymlinkCapabilityError, match="owner/repo"):
            downloader.download_subdirectory_package(dep, tmp_path / "out")

    sparse.assert_not_called()
    clone.assert_not_called()


def test_shared_bare_materialization_names_package(
    downloader: GitHubPackageDownloader, tmp_path: Path
) -> None:
    dep = DependencyReference(
        repo_url="owner/repo",
        reference=_SHA,
        virtual_path="skills/demo",
        is_virtual=True,
    )
    shared_cache = MagicMock()
    bare = tmp_path / "bare"
    bare.mkdir()
    shared_cache.get_or_clone.return_value = bare
    downloader.shared_clone_cache = shared_cache
    seen: dict[str, Any] = {}

    def materialize(_bare: Path, _consumer: Path, **kwargs: Any) -> str:
        seen.update(kwargs)
        raise GitSymlinkCapabilityError(kwargs["package"])

    with (
        patch.object(downloader, "_materialize_from_bare", side_effect=materialize),
        patch.object(downloader, "_git_env_dict", return_value={}),
        patch("apm_cli.deps.github_downloader.tempfile.mkdtemp", return_value=str(tmp_path)),
        patch("apm_cli.deps.github_downloader._rmtree"),
        pytest.raises(GitSymlinkCapabilityError) as raised,
    ):
        downloader.download_subdirectory_package(dep, tmp_path / "out")

    assert seen["package"] == str(dep)
    assert str(dep) in str(raised.value)


@pytest.mark.parametrize("returncode", [0, 1])
def test_sparse_checkout_link_failure_escapes_instead_of_reclone(
    downloader: GitHubPackageDownloader, tmp_path: Path, returncode: int
) -> None:
    """A sparse checkout that could not create a link must not fall back to a clone."""
    dep = DependencyReference(repo_url="owner/repo", reference="main")
    downloader._strategies.build_repo_url = MagicMock(return_value="https://github.com/o/r")
    downloader.git_env = {}
    ctx = MagicMock()
    ctx.auth_scheme = "basic"
    ctx.git_env = {}
    downloader.auth_resolver.resolve_for_dep.return_value = ctx
    downloader.auth_resolver.git_env_for_remote.side_effect = lambda auth_ctx, _url: dict(
        auth_ctx.git_env
    )
    failed = MagicMock(returncode=returncode)
    failed.stderr = "error: unable to create symlink skills/demo/AGENTS.md: Permission denied\n"
    ok = MagicMock(returncode=0, stderr="")

    def run(args: list[str], *_a: Any, **_k: Any) -> MagicMock:
        return failed if "checkout" in args and "FETCH_HEAD" in args else ok

    with (
        patch("apm_cli.deps.github_downloader.subprocess.run", side_effect=run),
        pytest.raises(GitSymlinkCapabilityError, match="owner/repo"),
    ):
        downloader._try_sparse_checkout(dep, tmp_path / "sparse", "skills/demo", "main")


def test_commit_clone_retries_without_default_branch_checkout(
    downloader: GitHubPackageDownloader, tmp_path: Path
) -> None:
    """A link at the default-branch HEAD retries with only the pinned commit materialized."""
    dep = DependencyReference(repo_url="owner/repo", reference=_SHA)
    resolved = ResolvedReference(
        original_ref=_SHA, ref_name=_SHA, ref_type=GitReferenceType.COMMIT, resolved_commit=_SHA
    )
    downloader.persistent_git_cache = None

    with ExitStack() as stack:
        _no_artifactory(stack, downloader)
        stack.enter_context(
            patch.object(downloader, "resolve_git_reference", return_value=resolved)
        )
        clone = stack.enter_context(
            patch.object(
                downloader,
                "_clone_with_fallback",
                side_effect=[GitSymlinkCapabilityError("owner/repo"), MagicMock()],
            )
        )
        checkout = stack.enter_context(
            patch("apm_cli.deps.github_downloader.checkout_git_worktree")
        )
        stack.enter_context(patch("apm_cli.deps.github_downloader._rmtree"))
        stack.enter_context(
            patch("apm_cli.deps.github_downloader.validate_apm_package", return_value=MagicMock())
        )
        stack.enter_context(patch("apm_cli.deps._shared._validate_and_load_package"))
        stack.enter_context(patch("apm_cli.deps.package_validator.stamp_plugin_version"))
        downloader.download_package(dep, tmp_path / "pkg")

    assert clone.call_count == 2
    assert "no_checkout" not in clone.call_args_list[0].kwargs
    assert clone.call_args_list[1].kwargs.get("no_checkout") is True
    assert checkout.call_args.args[1] == _SHA
