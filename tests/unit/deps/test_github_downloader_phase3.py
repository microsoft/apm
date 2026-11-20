"""Comprehensive unit tests for github_downloader.py - phase-3 coverage push.

Target: push coverage from ~54 % to >= 85 %.

All HTTP requests, git subprocess calls, and filesystem mutations are mocked so
the suite is fully hermetic and requires no network access or git installation.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.cache.git_cache import GitCache
from apm_cli.deps.github_downloader import (
    GitHubPackageDownloader,
)
from apm_cli.deps.tiered_ref_resolver import RefFreshnessPolicy, build_tiered_ref_resolver
from apm_cli.models.apm_package import (
    DependencyReference,
    GitReferenceType,
    ResolvedReference,
)


@pytest.fixture(autouse=True)
def _mock_git_object_links():
    """Mocked clones carry no Git objects; real-git tests cover link materialization."""
    with (
        patch("apm_cli.deps.github_downloader.materialize_tracked_symlinks"),
        patch("apm_cli.deps.github_downloader.validate_materialized_symlinks"),
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("remote_observation", [True, False])
@pytest.mark.parametrize("anonymous_first", [True, False])
def test_persistent_cache_only_records_authoritative_named_ref(
    tmp_path: Path, remote_observation: bool, anonymous_first: bool
) -> None:
    """Both auth paths record successful fresh observations, never old lock pins."""
    downloader = GitHubPackageDownloader.__new__(GitHubPackageDownloader)
    downloader.auth_resolver = MagicMock()
    downloader.auth_resolver.uses_public_github_anonymous_first.return_value = anonymous_first
    downloader.auth_resolver.try_with_fallback.side_effect = lambda host, operation, **kwargs: (
        operation(None, {})
    )
    downloader.git_env = {}
    downloader._cache_git_env = MagicMock(return_value={})
    downloader._tiered_resolver = MagicMock()
    downloader._tiered_resolver.remotely_resolved.return_value = remote_observation
    dependency = DependencyReference.parse("owner/repo#main")
    cache = MagicMock()
    cache.get_checkout.return_value = tmp_path
    url = dependency.to_github_url()
    assert (
        downloader._persistent_cache_checkout(cache, dependency, url, "b" * 40, locked_sha="b" * 40)
        == tmp_path
    )
    cache.get_checkout.assert_called_once()
    if remote_observation:
        cache.remember_resolved_ref.assert_called_once_with(url, "main", "b" * 40)
    else:
        cache.remember_resolved_ref.assert_not_called()


def _make_dep(
    repo_url: str = "owner/repo",
    *,
    host: str | None = None,
    reference: str | None = "main",
    virtual_path: str | None = None,
    is_virtual: bool = False,
    artifactory_prefix: str | None = None,
) -> DependencyReference:
    return DependencyReference(
        repo_url=repo_url,
        host=host,
        reference=reference,
        virtual_path=virtual_path,
        is_virtual=is_virtual,
        artifactory_prefix=artifactory_prefix,
    )


def _make_resolved(
    ref: str = "main",
    *,
    ref_type: GitReferenceType = GitReferenceType.BRANCH,
    commit: str | None = "abc1234" * 5 + "ab",
) -> ResolvedReference:
    return ResolvedReference(
        original_ref=ref,
        ref_name=ref,
        ref_type=ref_type,
        resolved_commit=commit,
    )


@pytest.fixture
def downloader() -> GitHubPackageDownloader:
    """Downloader with a fully-stubbed auth resolver (no token, no network)."""
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
    auth._token_manager = MagicMock()
    auth._token_manager.get_token_for_purpose.return_value = None
    return GitHubPackageDownloader(auth_resolver=auth)


@pytest.mark.parametrize("anonymous_first", [True, False])
@pytest.mark.parametrize(
    "case",
    [
        "locked-match",
        "locked-sibling",
        "locked-stale-provider",
        "locked-unrelated-stale-provider",
        "current-api",
        "current-legacy",
        "sha-passthrough",
        "failed-resolution",
        "mismatched-repository",
        "mismatched-ref",
        "mismatched-sha",
        "failed-checkout",
    ],
)
def test_scoped_lock_seeds_and_current_remote_receipts_remain_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    downloader: GitHubPackageDownloader,
    anonymous_first: bool,
    case: str,
) -> None:
    """A real resolver and receipt store agree across both checkout auth routes."""
    monkeypatch.setenv("APM_TIERED_RESOLVER", "1")
    old_sha, prior_sha, current_sha = "a" * 40, "b" * 40, "c" * 40
    dependency = _make_dep()
    stale_provider = case in {"locked-stale-provider", "locked-unrelated-stale-provider"}
    if stale_provider:
        monkeypatch.setenv("GITHUB_HOST", "code.example.com")
        dependency = _make_dep(host="code.example.com")
    cache = GitCache(tmp_path / "cache")
    original_url = dependency.to_github_url()
    cache.remember_resolved_ref(original_url, "main", prior_sha)
    downloader.auth_resolver.uses_public_github_anonymous_first.return_value = anonymous_first
    downloader.auth_resolver.try_with_fallback.side_effect = lambda host, operation, **kwargs: (
        operation(None, {})
    )
    downloader._cache_git_env = MagicMock(return_value={})
    downloader._refs = MagicMock()
    downloader._refs.resolve_commit_sha_for_ref.return_value = (
        None if case in {"current-legacy", "failed-resolution"} else current_sha
    )
    downloader._refs.resolve.return_value = _make_resolved(commit=current_sha)
    if case == "failed-resolution":
        downloader._refs.resolve.side_effect = RuntimeError("fixture remote unavailable")
    resolver = build_tiered_ref_resolver(
        downloader=downloader,
        git_cache=cache,
        freshness_policy=(
            RefFreshnessPolicy.LOCKED_OR_CURRENT
            if case.startswith("locked-")
            else RefFreshnessPolicy.CURRENT_REMOTE
        ),
    )
    assert resolver is not None
    downloader._tiered_resolver = resolver
    if stale_provider:
        seeded = DependencyReference(
            repo_url=(
                dependency.repo_url if case == "locked-stale-provider" else "removed/package"
            ),
            host="code.example.com",
            host_type="gitlab",
            reference="main",
        )
        assert resolver.seed(seeded, "main", old_sha) is False
        assert resolver._lock_seeds == {}
    elif case.startswith("locked-"):
        seeded = (
            dependency
            if case == "locked-match"
            else _make_dep(virtual_path="skills/sibling", is_virtual=True)
        )
        assert resolver.seed(seeded, "main", old_sha)
    if case == "sha-passthrough":
        dependency = _make_dep(reference=old_sha)
    if case == "failed-resolution":
        with pytest.raises(RuntimeError, match="fixture remote unavailable"):
            resolver.resolve(dependency)
        assert resolver.remotely_resolved(dependency, current_sha) is False
        assert cache.read_resolved_ref(original_url, "main") == (True, prior_sha)
        return
    resolved = downloader.resolve_git_reference(dependency)
    expected_sha = old_sha if case in {"locked-match", "sha-passthrough"} else current_sha
    assert resolved.resolved_commit == expected_sha
    if case == "locked-match":
        downloader._refs.resolve_commit_sha_for_ref.assert_not_called()
        downloader._refs.resolve.assert_not_called()
    if case == "locked-sibling":
        downloader._refs.resolve_commit_sha_for_ref.assert_called_once_with(dependency, "main")
        assert resolver.resolve(seeded).resolved_commit == old_sha
        assert resolver.remotely_resolved(seeded, old_sha) is False
    if stale_provider:
        downloader._refs.resolve_commit_sha_for_ref.assert_called_once_with(dependency, "main")
        downloader._refs.resolve.assert_not_called()
    if case == "mismatched-repository":
        dependency = _make_dep(repo_url="other/repository")
    elif case == "mismatched-ref":
        dependency = _make_dep(reference="other")
    elif case == "mismatched-sha":
        expected_sha = old_sha
    url = dependency.to_github_url()
    before_receipt = cache.read_resolved_ref(url, dependency.reference)
    authorized = case in {"current-api", "current-legacy", "failed-checkout"}
    assert resolver.remotely_resolved(dependency, expected_sha) is authorized

    def checkout(*args: object, **kwargs: object) -> Path:
        assert cache.read_resolved_ref(url, dependency.reference) == before_receipt
        if case == "failed-checkout":
            raise RuntimeError("fixture checkout failed")
        return tmp_path

    with patch.object(cache, "get_checkout", side_effect=checkout) as get_checkout:
        if case == "failed-checkout":
            with pytest.raises(RuntimeError, match="fixture checkout failed"):
                downloader._persistent_cache_checkout(
                    cache, dependency, url, expected_sha, locked_sha=expected_sha
                )
        else:
            assert (
                downloader._persistent_cache_checkout(
                    cache, dependency, url, expected_sha, locked_sha=expected_sha
                )
                == tmp_path
            )
        get_checkout.assert_called_once()
    assert cache.read_resolved_ref(url, dependency.reference) == (
        (True, current_sha) if case in {"current-api", "current-legacy"} else before_receipt
    )
    assert cache.read_resolved_ref(original_url, "main") == (
        True,
        current_sha if case in {"current-api", "current-legacy"} else prior_sha,
    )


# ---------------------------------------------------------------------------
# _debug
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _close_repo
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GitProgressReporter.update
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GitProgressReporter._get_op_name
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _is_generic_dependency_host
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _resolve_dep_token
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _resolve_dep_auth_ctx
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Backward-compat stubs (single-call delegation)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# registry_config property
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _get_clone_engine lazy construction
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# resolve_git_reference with tiered resolver
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# download_raw_file routing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _download_github_file gitlab routing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# validate_virtual_package_exists / shim methods
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# download_virtual_file_package - error paths
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# download_subdirectory_package - error paths
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _try_sparse_checkout
# ---------------------------------------------------------------------------


class TestTrySparseCheckout:
    def test_sparse_checkout_materializes_links_leniently(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        """The sparse clone hands its links to the Git-object owner (#3457)."""
        dep = _make_dep()
        downloader._strategies.build_repo_url = MagicMock(return_value="https://github.com/o/r")
        downloader.git_env = {}
        ctx = MagicMock()
        ctx.auth_scheme = "basic"
        ctx.git_env = {"MARK": "1"}
        downloader.auth_resolver.resolve_for_dep.return_value = ctx
        downloader.auth_resolver.git_env_for_remote.side_effect = lambda auth_ctx, _remote_url: (
            dict(auth_ctx.git_env)
        )
        clone_path = tmp_path / "sparse"
        ok_result = MagicMock(returncode=0)

        with (
            patch("apm_cli.deps.github_downloader.subprocess.run", return_value=ok_result),
            patch(
                "apm_cli.utils.git_env.init_git_remote_worktree",
                side_effect=lambda _path, _url, env, **_kw: env,
            ),
            patch("apm_cli.deps.github_downloader.apply_sparse_cone"),
            patch("apm_cli.deps.github_downloader.materialize_tracked_symlinks") as materialize,
        ):
            result = downloader._try_sparse_checkout(dep, clone_path, "skills/foo", "main")

        assert result is True
        args, kwargs = materialize.call_args
        assert args[1:] == (clone_path, ["skills/foo"])
        assert kwargs["strict"] is False
        assert kwargs["env"]["MARK"] == "1"


# ---------------------------------------------------------------------------
# download_package routing
# ---------------------------------------------------------------------------


class TestDownloadPackage:
    def test_cache_hit_with_unrecognized_schema_falls_back_without_cloning(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        from apm_cli.models.validation import PackageType

        dep = _make_dep()
        resolved = _make_resolved(ref_type=GitReferenceType.BRANCH)
        cached_dir = tmp_path / "cached"
        cached_dir.mkdir()
        (cached_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/2.0.0/plugin.schema.json",
                    "name": "future.plugin",
                }
            ),
            encoding="utf-8",
        )
        cache = MagicMock()
        cache.get_checkout.return_value = cached_dir
        downloader.persistent_git_cache = cache
        clone = MagicMock()

        target = tmp_path / "pkg"
        with (
            patch.object(downloader, "_is_artifactory_only", return_value=False),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            patch.object(downloader, "_should_use_artifactory_proxy", return_value=False),
            patch.object(downloader, "resolve_git_reference", return_value=resolved),
            patch.object(downloader, "_clone_with_fallback", clone),
        ):
            result = downloader.download_package(dep, target)

        assert result.package_type == PackageType.MARKETPLACE_PLUGIN
        assert result.package.name == "future.plugin"
        assert (target / "apm.yml").exists()
        clone.assert_not_called()

    def test_native_agent_plugin_clone_returns_projected_package_without_apm_yml(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        from apm_cli.agent_plugins import PLUGIN_SCHEMA_ID
        from apm_cli.models.validation import PackageType

        dep = _make_dep()
        resolved = _make_resolved(ref_type=GitReferenceType.BRANCH)
        target = tmp_path / "pkg"

        def clone_native(repo_url, clone_path, **kwargs):
            del repo_url, kwargs
            clone_path.mkdir(parents=True, exist_ok=True)
            (clone_path / "plugin.json").write_text(
                json.dumps(
                    {
                        "$schema": PLUGIN_SCHEMA_ID,
                        "name": "native.plugin",
                        "version": "1.0.0",
                    }
                ),
                encoding="utf-8",
            )
            return MagicMock()

        with (
            patch.object(downloader, "_is_artifactory_only", return_value=False),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            patch.object(downloader, "_should_use_artifactory_proxy", return_value=False),
            patch.object(downloader, "resolve_git_reference", return_value=resolved),
            patch.object(downloader, "_clone_with_fallback", side_effect=clone_native),
        ):
            result = downloader.download_package(dep, target)

        assert result.package_type == PackageType.AGENT_PLUGIN
        assert result.package.name == "native.plugin"
        assert result.package.agent_plugin is not None
        assert not (target / "apm.yml").exists()

    def test_unrecognized_agent_plugin_schema_clone_falls_back_to_legacy_projection(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        from apm_cli.models.validation import PackageType

        dep = _make_dep()
        resolved = _make_resolved(ref_type=GitReferenceType.BRANCH)
        target = tmp_path / "pkg"

        def clone_unsupported(repo_url, clone_path, **kwargs):
            del repo_url, kwargs
            clone_path.mkdir(parents=True, exist_ok=True)
            (clone_path / "plugin.json").write_text(
                json.dumps(
                    {
                        "$schema": ("https://agent-plugins.org/schemas/2.0.0/plugin.schema.json"),
                        "name": "future.plugin",
                    }
                ),
                encoding="utf-8",
            )
            return MagicMock()

        with (
            patch.object(downloader, "_is_artifactory_only", return_value=False),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            patch.object(downloader, "_should_use_artifactory_proxy", return_value=False),
            patch.object(downloader, "resolve_git_reference", return_value=resolved),
            patch.object(downloader, "_clone_with_fallback", side_effect=clone_unsupported),
        ):
            result = downloader.download_package(dep, target)

        assert result.package_type == PackageType.MARKETPLACE_PLUGIN
        assert result.package.name == "future.plugin"
        assert (target / "apm.yml").exists()


# ---------------------------------------------------------------------------
# _get_clone_progress_callback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _sanitize_git_error
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Artifactory backward-compat stubs
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Persistent git cache paths in download_package
# ---------------------------------------------------------------------------
