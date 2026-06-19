"""Integration tests for ``apm_cli.deps.git_reference_resolver`` -- phase 3 wave 5.

The resolver collaborates with the downloader context, git, auth helpers, and
filesystem staging. These tests keep all external I/O mocked while exercising
real control flow.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from git.exc import GitCommandError

from apm_cli.deps.git_reference_resolver import GitReferenceResolver
from apm_cli.models.apm_package import DependencyReference, GitReferenceType


def _make_host() -> MagicMock:
    host = MagicMock()
    host.git_env = {"GIT_TERMINAL_PROMPT": "0"}
    host.auth_resolver = MagicMock()
    host.auth_resolver._build_git_env.return_value = {"AUTH": "bearer"}
    host.auth_resolver.execute_with_bearer_fallback.side_effect = (
        lambda dep_ref, primary, bearer, is_auth_failure: MagicMock(
            outcome=primary(),
            bearer_attempted=False,
        )
    )
    host.auth_resolver.build_error_context.return_value = "auth help"
    host.auth_resolver.classify_host.return_value = MagicMock(display_name="Generic Git")
    host.auth_resolver.resolve.return_value = MagicMock(token="mytoken")
    host.shared_clone_cache = None
    host._resolve_dep_token.return_value = "mytoken"
    host._resolve_dep_auth_ctx.return_value = MagicMock(auth_scheme="basic", git_env={"CTX": "1"})
    host._build_noninteractive_git_env.return_value = {"NONINTERACTIVE": "1"}
    host._build_repo_url.return_value = "https://github.com/owner/repo.git"
    host._sanitize_git_error.side_effect = lambda x: x
    host._resilient_get.return_value = MagicMock(status_code=200, text="abc" * 14)
    host._parse_ls_remote_output.return_value = []
    host._sort_remote_refs.side_effect = lambda refs: refs
    host._parse_artifactory_base_url.return_value = None
    host._should_use_artifactory_proxy.return_value = False
    host._clone_with_fallback = MagicMock()
    return host


def _make_dep(
    *,
    repo_url: str = "owner/repo",
    host_name: str | None = "github.com",
    reference: str | None = "main",
    port: int | None = None,
    artifactory: bool = False,
    ado: bool = False,
    insecure: bool = False,
) -> MagicMock:
    dep = MagicMock(spec=DependencyReference)
    dep.is_artifactory.return_value = artifactory
    dep.is_azure_devops.return_value = ado
    dep.repo_url = repo_url
    dep.host = host_name
    dep.port = port
    dep.reference = reference
    dep.is_insecure = insecure
    dep.__str__.return_value = f"{repo_url}#{reference}" if reference else repo_url
    return dep


class TestListRemoteRefs:
    def test_artifactory_returns_empty_list(self) -> None:
        host = _make_host()
        dep = _make_dep(artifactory=True)

        assert GitReferenceResolver(host).list_remote_refs(dep) == []

    def test_unauthenticated_uses_noninteractive_env(self) -> None:
        host = _make_host()
        host._resolve_dep_token.return_value = None
        host._resolve_dep_auth_ctx.return_value = None
        host._parse_ls_remote_output.return_value = [MagicMock()]
        mock_git = MagicMock()
        mock_git.ls_remote.return_value = "a" * 40 + "\trefs/heads/main\n"

        with patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git):
            refs = GitReferenceResolver(host).list_remote_refs(_make_dep())

        assert refs == host._parse_ls_remote_output.return_value
        host._build_noninteractive_git_env.assert_called_once_with(
            preserve_config_isolation=False,
            suppress_credential_helpers=False,
        )
        assert mock_git.ls_remote.call_args.kwargs["env"] == {"NONINTERACTIVE": "1"}

    def test_basic_token_uses_host_git_env(self) -> None:
        host = _make_host()
        host._parse_ls_remote_output.return_value = [MagicMock()]
        mock_git = MagicMock()
        mock_git.ls_remote.return_value = "b" * 40 + "\trefs/heads/main\n"

        with patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git):
            GitReferenceResolver(host).list_remote_refs(_make_dep())

        assert mock_git.ls_remote.call_args.kwargs["env"] == host.git_env

    def test_bearer_token_uses_context_git_env(self) -> None:
        host = _make_host()
        host._resolve_dep_auth_ctx.return_value = MagicMock(
            auth_scheme="bearer",
            git_env={"BEARER": "1"},
        )
        host._parse_ls_remote_output.return_value = [MagicMock()]
        mock_git = MagicMock()
        mock_git.ls_remote.return_value = "c" * 40 + "\trefs/heads/main\n"

        with patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git):
            GitReferenceResolver(host).list_remote_refs(_make_dep())

        assert mock_git.ls_remote.call_args.kwargs["env"] == {"BEARER": "1"}

    def test_insecure_dependency_preserves_config_isolation_flags(self) -> None:
        host = _make_host()
        host._resolve_dep_token.return_value = None
        host._resolve_dep_auth_ctx.return_value = None
        mock_git = MagicMock()
        mock_git.ls_remote.return_value = "d" * 40 + "\trefs/heads/main\n"

        with patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git):
            GitReferenceResolver(host).list_remote_refs(_make_dep(insecure=True))

        host._build_noninteractive_git_env.assert_called_once_with(
            preserve_config_isolation=True,
            suppress_credential_helpers=True,
        )

    def test_success_parses_and_sorts_refs(self) -> None:
        host = _make_host()
        parsed_refs = [MagicMock(name="branch")]
        sorted_refs = [MagicMock(name="sorted")]
        host._parse_ls_remote_output.return_value = parsed_refs
        host._sort_remote_refs.side_effect = lambda refs: sorted_refs
        mock_git = MagicMock()
        output = "e" * 40 + "\trefs/heads/main\n"
        mock_git.ls_remote.return_value = output

        with patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git):
            refs = GitReferenceResolver(host).list_remote_refs(_make_dep())

        assert refs == sorted_refs
        host._parse_ls_remote_output.assert_called_once_with(output)
        host._sort_remote_refs.assert_called_once_with(parsed_refs)

    def test_ado_basic_token_uses_bearer_fallback(self) -> None:
        host = _make_host()
        dep = _make_dep(host_name="dev.azure.com", ado=True)
        mock_git = MagicMock()
        auth_error = GitCommandError("ls-remote", 128, stderr="Authentication failed")

        def _ls_remote(*args, **kwargs):
            env = kwargs["env"]
            if env == host.git_env:
                raise auth_error
            return "f" * 40 + "\trefs/heads/main\n"

        def _fallback(dep_ref, primary, bearer, is_auth_failure):
            primary_outcome = primary()
            assert is_auth_failure(primary_outcome) is True
            outcome = bearer("jwt-token")
            return MagicMock(outcome=outcome, bearer_attempted=True)

        host.auth_resolver.execute_with_bearer_fallback.side_effect = _fallback
        mock_git.ls_remote.side_effect = _ls_remote
        host._parse_ls_remote_output.return_value = [MagicMock()]

        with patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git):
            refs = GitReferenceResolver(host).list_remote_refs(dep)

        assert refs == host._parse_ls_remote_output.return_value
        host.auth_resolver.execute_with_bearer_fallback.assert_called_once()
        host.auth_resolver._build_git_env.assert_called_once_with(
            "jwt-token",
            scheme="bearer",
            host_kind="ado",
        )

    def test_ado_without_token_skips_bearer_fallback(self) -> None:
        host = _make_host()
        host._resolve_dep_token.return_value = None
        dep = _make_dep(host_name="dev.azure.com", ado=True)
        mock_git = MagicMock()
        mock_git.ls_remote.return_value = "1" * 40 + "\trefs/heads/main\n"

        with patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git):
            GitReferenceResolver(host).list_remote_refs(dep)

        host.auth_resolver.execute_with_bearer_fallback.assert_not_called()

    def test_generic_host_error_mentions_ssh_and_credential_helper(self) -> None:
        host = _make_host()
        host.auth_resolver.classify_host.return_value = MagicMock(display_name="Bitbucket Server")
        dep = _make_dep(host_name="bitbucket.example.com")
        mock_git = MagicMock()
        mock_git.ls_remote.side_effect = GitCommandError("ls-remote", 128, stderr="denied")

        with (
            patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git),
            patch("apm_cli.deps.git_reference_resolver.is_github_hostname", return_value=False),
        ):
            with pytest.raises(RuntimeError, match="Bitbucket Server") as exc_info:
                GitReferenceResolver(host).list_remote_refs(dep)

        assert "configure SSH keys or a git credential helper" in str(exc_info.value)

    def test_missing_host_uses_default_host_for_auth_context(self) -> None:
        host = _make_host()
        dep = _make_dep(host_name=None)
        mock_git = MagicMock()
        mock_git.ls_remote.side_effect = GitCommandError("ls-remote", 128, stderr="denied")

        with (
            patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git),
            patch(
                "apm_cli.deps.git_reference_resolver.default_host",
                return_value="default.example.com",
            ),
            pytest.raises(RuntimeError, match="auth help"),
        ):
            GitReferenceResolver(host).list_remote_refs(dep)

        host.auth_resolver.build_error_context.assert_called_once_with(
            "default.example.com",
            "list refs",
            org="owner",
            port=None,
            dep_url="owner/repo",
            bearer_also_failed=False,
        )

    def test_github_host_error_uses_auth_resolver_context(self) -> None:
        host = _make_host()
        dep = _make_dep(repo_url="octo/repo", host_name="github.example.com")
        mock_git = MagicMock()
        mock_git.ls_remote.side_effect = GitCommandError("ls-remote", 128, stderr="denied")

        with (
            patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git),
            patch("apm_cli.deps.git_reference_resolver.is_github_hostname", return_value=True),
            pytest.raises(RuntimeError, match="auth help"),
        ):
            GitReferenceResolver(host).list_remote_refs(dep)

        host.auth_resolver.build_error_context.assert_called_once_with(
            "github.example.com",
            "list refs",
            org="octo",
            port=None,
            dep_url="octo/repo",
            bearer_also_failed=False,
        )

    def test_ado_bearer_failure_sets_bearer_also_failed_flag(self) -> None:
        host = _make_host()
        dep = _make_dep(host_name="dev.azure.com", repo_url="org/repo", ado=True)
        mock_git = MagicMock()
        auth_error = GitCommandError("ls-remote", 128, stderr="Authentication failed")

        def _fallback(dep_ref, primary, bearer, is_auth_failure):
            primary()
            return MagicMock(outcome=("err", auth_error), bearer_attempted=True)

        host.auth_resolver.execute_with_bearer_fallback.side_effect = _fallback
        mock_git.ls_remote.side_effect = auth_error

        with (
            patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git),
            pytest.raises(RuntimeError),
        ):
            GitReferenceResolver(host).list_remote_refs(dep)

        assert host.auth_resolver.build_error_context.call_args.kwargs["bearer_also_failed"] is True

    def test_error_message_includes_sanitized_git_error(self) -> None:
        host = _make_host()
        host._sanitize_git_error.side_effect = lambda _text: "[sanitized]"
        mock_git = MagicMock()
        mock_git.ls_remote.side_effect = GitCommandError("ls-remote", 128, stderr="secret")

        with (
            patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git),
            pytest.raises(RuntimeError, match=r"\[sanitized\]"),
        ):
            GitReferenceResolver(host).list_remote_refs(_make_dep())


class TestResolveCommitShaForRef:
    def test_artifactory_short_circuits_to_none(self) -> None:
        host = _make_host()
        assert (
            GitReferenceResolver(host).resolve_commit_sha_for_ref(
                _make_dep(artifactory=True), "main"
            )
            is None
        )

    def test_ado_short_circuits_to_none(self) -> None:
        host = _make_host()
        assert (
            GitReferenceResolver(host).resolve_commit_sha_for_ref(_make_dep(ado=True), "main")
            is None
        )

    def test_dependency_probe_exception_returns_none(self) -> None:
        host = _make_host()
        dep = _make_dep()
        dep.is_artifactory.side_effect = RuntimeError("bad dep")

        assert GitReferenceResolver(host).resolve_commit_sha_for_ref(dep, "main") is None

    def test_full_sha_bypasses_backend_lookup_and_lowercases(self) -> None:
        host = _make_host()
        sha = "A" * 40

        with patch("apm_cli.deps.host_backends.backend_for") as mock_backend:
            result = GitReferenceResolver(host).resolve_commit_sha_for_ref(_make_dep(), sha)

        assert result == sha.lower()
        mock_backend.assert_not_called()

    def test_invalid_repo_url_returns_none(self) -> None:
        host = _make_host()
        dep = _make_dep()
        dep.repo_url = None

        assert GitReferenceResolver(host).resolve_commit_sha_for_ref(dep, "main") is None

    def test_backend_returning_none_api_url_returns_none(self) -> None:
        host = _make_host()
        backend = MagicMock()
        backend.build_commits_api_url.return_value = None

        with patch("apm_cli.deps.host_backends.backend_for", return_value=backend):
            result = GitReferenceResolver(host).resolve_commit_sha_for_ref(_make_dep(), "main")

        assert result is None

    def test_backend_for_uses_default_host_when_dependency_host_missing(self) -> None:
        host = _make_host()
        backend = MagicMock()
        backend.build_commits_api_url.return_value = None
        dep = _make_dep(host_name=None)

        with patch("apm_cli.deps.host_backends.backend_for", return_value=backend) as mock_backend:
            GitReferenceResolver(host).resolve_commit_sha_for_ref(dep, "main")

        assert mock_backend.call_args.kwargs["fallback_host"] == "github.com"

    def test_auth_resolver_failure_still_attempts_request_without_token(self) -> None:
        host = _make_host()
        host.auth_resolver.resolve.side_effect = RuntimeError("no token")
        host._resilient_get.return_value = MagicMock(status_code=200, text="b" * 40)
        backend = MagicMock()
        backend.build_commits_api_url.return_value = (
            "https://api.github.com/repos/owner/repo/commits/main"
        )

        with patch("apm_cli.deps.host_backends.backend_for", return_value=backend):
            result = GitReferenceResolver(host).resolve_commit_sha_for_ref(_make_dep(), "main")

        assert result == "b" * 40
        assert "Authorization" not in host._resilient_get.call_args.kwargs["headers"]

    def test_success_returns_lowercased_sha_and_authorization_header(self) -> None:
        host = _make_host()
        backend = MagicMock()
        backend.build_commits_api_url.return_value = (
            "https://api.github.com/repos/owner/repo/commits/main"
        )
        host._resilient_get.return_value = MagicMock(status_code=200, text="C" * 40)

        with patch("apm_cli.deps.host_backends.backend_for", return_value=backend):
            result = GitReferenceResolver(host).resolve_commit_sha_for_ref(_make_dep(), "main")

        assert result == "c" * 40
        headers = host._resilient_get.call_args.kwargs["headers"]
        assert headers["Accept"] == "application/vnd.github.sha"
        assert headers["Authorization"] == "token mytoken"

    def test_non_200_response_returns_none(self) -> None:
        host = _make_host()
        backend = MagicMock()
        backend.build_commits_api_url.return_value = (
            "https://api.github.com/repos/owner/repo/commits/main"
        )
        host._resilient_get.return_value = MagicMock(status_code=404, text="not found")

        with patch("apm_cli.deps.host_backends.backend_for", return_value=backend):
            assert (
                GitReferenceResolver(host).resolve_commit_sha_for_ref(_make_dep(), "main") is None
            )

    def test_non_sha_body_returns_none(self) -> None:
        host = _make_host()
        backend = MagicMock()
        backend.build_commits_api_url.return_value = (
            "https://api.github.com/repos/owner/repo/commits/main"
        )
        host._resilient_get.return_value = MagicMock(status_code=200, text="not-a-sha")

        with patch("apm_cli.deps.host_backends.backend_for", return_value=backend):
            assert (
                GitReferenceResolver(host).resolve_commit_sha_for_ref(_make_dep(), "main") is None
            )

    def test_request_exception_returns_none(self) -> None:
        host = _make_host()
        backend = MagicMock()
        backend.build_commits_api_url.return_value = (
            "https://api.github.com/repos/owner/repo/commits/main"
        )
        host._resilient_get.side_effect = RuntimeError("network down")

        with patch("apm_cli.deps.host_backends.backend_for", return_value=backend):
            assert (
                GitReferenceResolver(host).resolve_commit_sha_for_ref(_make_dep(), "main") is None
            )


class TestResolve:
    def test_invalid_string_reference_wraps_parse_error(self) -> None:
        host = _make_host()

        with (
            patch(
                "apm_cli.deps.git_reference_resolver.DependencyReference.parse",
                side_effect=ValueError("bad syntax"),
            ),
            pytest.raises(ValueError, match="Invalid repository reference 'bad-ref': bad syntax"),
        ):
            GitReferenceResolver(host).resolve("bad-ref")

    def test_string_reference_uses_dependency_parse_result(self) -> None:
        host = _make_host()
        dep = _make_dep(reference=None, artifactory=True)

        with patch(
            "apm_cli.deps.git_reference_resolver.DependencyReference.parse", return_value=dep
        ):
            result = GitReferenceResolver(host).resolve("owner/repo")

        assert result.ref_name == "main"
        assert result.ref_type == GitReferenceType.BRANCH

    def test_artifactory_without_ref_defaults_to_main(self) -> None:
        host = _make_host()
        dep = _make_dep(reference=None, artifactory=True)

        result = GitReferenceResolver(host).resolve(dep)

        assert result.ref_name == "main"
        assert result.ref_type == GitReferenceType.BRANCH
        assert result.resolved_commit is None

    def test_artifactory_commit_shaped_ref_returns_commit_type(self) -> None:
        host = _make_host()
        dep = _make_dep(reference="abcdef1", artifactory=True)

        result = GitReferenceResolver(host).resolve(dep)

        assert result.ref_name == "abcdef1"
        assert result.ref_type == GitReferenceType.COMMIT

    def test_artifactory_proxy_short_circuits_without_clone(self) -> None:
        host = _make_host()
        host._parse_artifactory_base_url.return_value = ("https://art.example.com", "repo")
        host._should_use_artifactory_proxy.return_value = True
        dep = _make_dep(reference="release")

        result = GitReferenceResolver(host).resolve(dep)

        assert result.ref_name == "release"
        assert result.ref_type == GitReferenceType.BRANCH
        host._clone_with_fallback.assert_not_called()

    def test_commit_reference_clones_and_resolves_commit_sha(self, tmp_path: Path) -> None:
        host = _make_host()
        dep = _make_dep(reference="a" * 40)
        repo = MagicMock()
        repo.commit.return_value = MagicMock(hexsha="b" * 40)
        host._clone_with_fallback.return_value = repo
        temp_dir = tmp_path / "commit"
        temp_dir.mkdir()

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value=tmp_path),
            patch(
                "apm_cli.deps.git_reference_resolver.tempfile.mkdtemp", return_value=str(temp_dir)
            ),
            patch("apm_cli.deps.github_downloader._rmtree") as mock_rmtree,
        ):
            result = GitReferenceResolver(host).resolve(dep)

        assert result.ref_type == GitReferenceType.COMMIT
        assert result.resolved_commit == "b" * 40
        mock_rmtree.assert_called_once_with(temp_dir)

    def test_commit_reference_failure_raises_value_error(self, tmp_path: Path) -> None:
        host = _make_host()
        dep = _make_dep(reference="a" * 40)
        host._clone_with_fallback.side_effect = RuntimeError("secret error")
        host._sanitize_git_error.side_effect = lambda _text: "sanitized error"
        temp_dir = tmp_path / "commit-fail"
        temp_dir.mkdir()

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value=tmp_path),
            patch(
                "apm_cli.deps.git_reference_resolver.tempfile.mkdtemp", return_value=str(temp_dir)
            ),
            patch("apm_cli.deps.github_downloader._rmtree"),
        ):
            with pytest.raises(
                ValueError, match="Could not resolve commit '" + ("a" * 40) + "'"
            ) as exc_info:
                GitReferenceResolver(host).resolve(dep)

        assert "sanitized error" in str(exc_info.value)

    def test_shallow_clone_success_with_explicit_branch(self, tmp_path: Path) -> None:
        host = _make_host()
        dep = _make_dep(reference="main")
        repo = MagicMock()
        repo.head.commit.hexsha = "c" * 40
        repo.active_branch.name = "ignored"
        host._clone_with_fallback.return_value = repo
        temp_dir = tmp_path / "branch"
        temp_dir.mkdir()

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value=tmp_path),
            patch(
                "apm_cli.deps.git_reference_resolver.tempfile.mkdtemp", return_value=str(temp_dir)
            ),
            patch("apm_cli.deps.github_downloader._rmtree"),
        ):
            result = GitReferenceResolver(host).resolve(dep)

        assert result.ref_type == GitReferenceType.BRANCH
        assert result.ref_name == "main"
        assert result.resolved_commit == "c" * 40
        assert host._clone_with_fallback.call_args.kwargs["depth"] == 1
        assert host._clone_with_fallback.call_args.kwargs["branch"] == "main"

    def test_shallow_clone_success_without_ref_uses_active_branch_name(
        self, tmp_path: Path
    ) -> None:
        host = _make_host()
        dep = _make_dep(reference=None)
        repo = MagicMock()
        repo.head.commit.hexsha = "d" * 40
        repo.active_branch.name = "develop"
        host._clone_with_fallback.return_value = repo
        temp_dir = tmp_path / "default-branch"
        temp_dir.mkdir()

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value=tmp_path),
            patch(
                "apm_cli.deps.git_reference_resolver.tempfile.mkdtemp", return_value=str(temp_dir)
            ),
            patch("apm_cli.deps.github_downloader._rmtree"),
        ):
            result = GitReferenceResolver(host).resolve(dep)

        assert result.ref_name == "develop"
        assert result.resolved_commit == "d" * 40
        assert "branch" not in host._clone_with_fallback.call_args.kwargs

    def test_shallow_clone_failure_falls_back_to_origin_branch_lookup(self, tmp_path: Path) -> None:
        host = _make_host()
        dep = _make_dep(reference="release")
        repo = MagicMock()
        branch = MagicMock()
        branch.commit.hexsha = "e" * 40
        repo.refs.__getitem__.return_value = branch
        host._clone_with_fallback.side_effect = [GitCommandError("clone", 1), repo]
        temp_dir = tmp_path / "fallback-branch"
        temp_dir.mkdir()

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value=tmp_path),
            patch(
                "apm_cli.deps.git_reference_resolver.tempfile.mkdtemp", return_value=str(temp_dir)
            ),
            patch("apm_cli.deps.github_downloader._rmtree"),
        ):
            result = GitReferenceResolver(host).resolve(dep)

        assert result.ref_type == GitReferenceType.BRANCH
        assert result.ref_name == "release"
        assert result.resolved_commit == "e" * 40

    def test_shallow_clone_fallback_resolves_tag_when_branch_missing(self, tmp_path: Path) -> None:
        host = _make_host()
        dep = _make_dep(reference="v1.2.3")
        repo = MagicMock()
        tag = MagicMock()
        tag.commit.hexsha = "f" * 40
        repo.refs.__getitem__.side_effect = IndexError("missing branch")
        repo.tags.__getitem__.return_value = tag
        host._clone_with_fallback.side_effect = [GitCommandError("clone", 1), repo]
        temp_dir = tmp_path / "fallback-tag"
        temp_dir.mkdir()

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value=tmp_path),
            patch(
                "apm_cli.deps.git_reference_resolver.tempfile.mkdtemp", return_value=str(temp_dir)
            ),
            patch("apm_cli.deps.github_downloader._rmtree"),
        ):
            result = GitReferenceResolver(host).resolve(dep)

        assert result.ref_type == GitReferenceType.TAG
        assert result.ref_name == "v1.2.3"
        assert result.resolved_commit == "f" * 40

    def test_shallow_clone_fallback_missing_reference_raises_value_error(
        self, tmp_path: Path
    ) -> None:
        host = _make_host()
        dep = _make_dep(reference="missing")
        repo = MagicMock()
        repo.refs.__getitem__.side_effect = IndexError("missing branch")
        repo.tags.__getitem__.side_effect = IndexError("missing tag")
        host._clone_with_fallback.side_effect = [GitCommandError("clone", 1), repo]
        temp_dir = tmp_path / "missing-ref"
        temp_dir.mkdir()

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value=tmp_path),
            patch(
                "apm_cli.deps.git_reference_resolver.tempfile.mkdtemp", return_value=str(temp_dir)
            ),
            patch("apm_cli.deps.github_downloader._rmtree"),
        ):
            with pytest.raises(
                ValueError, match="Could not resolve reference 'missing'"
            ) as exc_info:
                GitReferenceResolver(host).resolve(dep)

        assert "Reference 'missing' not found" in str(exc_info.value)

    def test_full_clone_auth_failure_raises_runtime_error_with_context(
        self, tmp_path: Path
    ) -> None:
        host = _make_host()
        dep = _make_dep(reference="release")
        auth_error = GitCommandError("clone", 1, stderr="Authentication failed")
        host._clone_with_fallback.side_effect = [GitCommandError("clone", 1), auth_error]
        temp_dir = tmp_path / "auth-fail"
        temp_dir.mkdir()

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value=tmp_path),
            patch(
                "apm_cli.deps.git_reference_resolver.tempfile.mkdtemp", return_value=str(temp_dir)
            ),
            patch("apm_cli.deps.github_downloader._rmtree"),
        ):
            with pytest.raises(
                RuntimeError, match="Failed to clone repository owner/repo"
            ) as exc_info:
                GitReferenceResolver(host).resolve(dep)

        assert "auth help" in str(exc_info.value)
        host.auth_resolver.build_error_context.assert_called_with(
            "github.com",
            "resolve reference",
            org="owner",
            port=None,
            dep_url="owner/repo",
        )

    def test_full_clone_non_auth_failure_raises_runtime_error(self, tmp_path: Path) -> None:
        host = _make_host()
        dep = _make_dep(reference="release")
        generic_error = GitCommandError("clone", 1, stderr="fatal: boom")
        host._clone_with_fallback.side_effect = [GitCommandError("clone", 1), generic_error]
        host._sanitize_git_error.side_effect = lambda _text: "sanitized failure"
        temp_dir = tmp_path / "generic-fail"
        temp_dir.mkdir()

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value=tmp_path),
            patch(
                "apm_cli.deps.git_reference_resolver.tempfile.mkdtemp", return_value=str(temp_dir)
            ),
            patch("apm_cli.deps.github_downloader._rmtree"),
            pytest.raises(RuntimeError, match="sanitized failure"),
        ):
            GitReferenceResolver(host).resolve(dep)

    def test_cleanup_skipped_when_temp_directory_missing(self, tmp_path: Path) -> None:
        host = _make_host()
        dep = _make_dep(reference=None, artifactory=True)
        temp_dir = tmp_path / "missing-temp"

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value=tmp_path),
            patch(
                "apm_cli.deps.git_reference_resolver.tempfile.mkdtemp", return_value=str(temp_dir)
            ),
            patch("apm_cli.deps.github_downloader._rmtree") as mock_rmtree,
        ):
            result = GitReferenceResolver(host).resolve(dep)

        assert result.ref_name == "main"
        mock_rmtree.assert_not_called()
