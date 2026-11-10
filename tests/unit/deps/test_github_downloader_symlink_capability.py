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
        clone = stack.enter_context(patch.object(downloader, "_clone_with_fallback"))
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
        clone = stack.enter_context(patch.object(downloader, "_clone_with_fallback"))
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
