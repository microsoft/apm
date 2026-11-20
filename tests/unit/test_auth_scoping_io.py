"""Unit tests for auth scoping: tokens are only sent to their matching hosts (filesystem, process and CLI boundary cases)."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import urlparse

import pytest
from git.exc import GitCommandError

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import APMPackage, DependencyReference
from apm_cli.utils.git_env import get_git_executable

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_downloader(github_token=None, ado_token=None, gitlab_token=None):
    """Create a GitHubPackageDownloader with controlled tokens."""
    with (
        patch.dict(
            os.environ,
            {
                **({"GITHUB_APM_PAT": github_token} if github_token else {}),
                **({"ADO_APM_PAT": ado_token} if ado_token else {}),
                **({"GITLAB_APM_PAT": gitlab_token} if gitlab_token else {}),
            },
            clear=True,
        ),
        patch(
            "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
            return_value=None,
        ),
    ):
        downloader = GitHubPackageDownloader()
    return downloader


def _dep(url_str):
    """Shortcut: parse a DependencyReference from a string."""
    return DependencyReference.parse(url_str)


def _url_host(url: str) -> str:
    """Extract the hostname from an HTTPS or SSH git URL."""
    parsed = urlparse(url)
    if parsed.hostname:
        return parsed.hostname
    # SSH shorthand: git@host:path
    if url.startswith("git@") and ":" in url:
        return url.split("@", 1)[1].split(":", 1)[0]
    raise ValueError(f"Cannot extract host from URL: {url}")


# ===========================================================================
# _build_repo_url - token scoping
# ===========================================================================


# ===========================================================================
# _clone_with_fallback - env relaxation for generic hosts
# ===========================================================================


class TestCloneWithFallbackEnv:
    """Verify that env lockdown is based on token availability, not host type."""

    def _run_clone(self, dl, dep, succeed_on=1):
        """Run _clone_with_fallback, succeeding on the nth attempt (1-based).

        Returns the list of Repo.clone_from call_args.
        """
        mock_repo = Mock()
        mock_repo.head.commit.hexsha = "abc123"

        effects = []
        for i in range(3):
            if i == succeed_on - 1:
                effects.append(mock_repo)
            else:
                effects.append(GitCommandError("clone", "failed"))

        # Reconstruct the env matching construction so per-dep resolution
        # via AuthResolver sees the same tokens the downloader was built with.
        env_vars = {}
        if dl.github_token:
            env_vars["GITHUB_APM_PAT"] = dl.github_token
        if dl.ado_token:
            env_vars["ADO_APM_PAT"] = dl.ado_token
        if getattr(dl, "gitlab_token", None):
            env_vars["GITLAB_APM_PAT"] = dl.gitlab_token

        # Clear the resolver cache so resolve_for_dep re-resolves with the
        # controlled env rather than returning stale entries.
        dl.auth_resolver._cache.clear()

        with (
            patch.dict(os.environ, env_vars, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
            patch("apm_cli.deps.github_downloader.Repo") as MockRepo,
        ):
            MockRepo.clone_from.side_effect = effects
            target = Path(tempfile.mkdtemp())
            try:
                dl._clone_with_fallback(dep.repo_url, target, dep_ref=dep)
            except RuntimeError:
                pass  # all methods failed is OK here
            finally:
                import shutil

                shutil.rmtree(target, ignore_errors=True)
            return MockRepo.clone_from.call_args_list

    def test_generic_host_env_allows_credential_helpers(self):
        """For generic hosts (e.g. Bitbucket), GIT_ASKPASS / GIT_CONFIG_GLOBAL are NOT set."""
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://bitbucket.org/acme/rules.git")

        calls = self._run_clone(dl, dep, succeed_on=1)
        assert len(calls) >= 1

        # Under strict default, explicit https:// -> single HTTPS attempt; env is relaxed
        # because no token was available for the generic host.
        env_used = calls[0][1].get("env", calls[0].kwargs.get("env"))
        assert "GIT_ASKPASS" not in env_used
        assert "GIT_CONFIG_GLOBAL" not in env_used
        assert "GIT_CONFIG_NOSYSTEM" not in env_used
        # But GIT_TERMINAL_PROMPT should still be set
        assert env_used.get("GIT_TERMINAL_PROMPT") == "0"

    def test_github_host_env_is_locked_down(self):
        """For GitHub hosts WITH a token, the locked-down env with GIT_ASKPASS etc. is used."""
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://github.com/owner/repo.git")

        calls = self._run_clone(dl, dep, succeed_on=1)
        assert len(calls) >= 1

        env_used = calls[0][1].get("env", calls[0].kwargs.get("env"))
        assert env_used.get("GIT_ASKPASS") == "echo"
        assert env_used.get("GIT_CONFIG_NOSYSTEM") == "1"
        cfg_path = env_used.get("GIT_CONFIG_GLOBAL")
        if sys.platform == "win32":
            assert cfg_path != "NUL"
            assert os.path.isfile(cfg_path)
        else:
            assert cfg_path == "/dev/null"

    def test_github_host_no_token_allows_credential_helpers(self):
        """GitHub anonymous-first blocks credential helpers before the request."""
        dl = _make_downloader(github_token=None)
        dep = _dep("https://github.com/owner/repo.git")

        calls = self._run_clone(dl, dep, succeed_on=1)
        assert len(calls) >= 1

        env_used = calls[0][1].get("env", calls[0].kwargs.get("env"))
        assert env_used.get("GIT_ASKPASS") == "echo"
        assert env_used.get("GIT_CONFIG_NOSYSTEM") == "1"
        assert env_used.get("GIT_CONFIG_KEY_0") == "credential.helper"
        assert env_used.get("GIT_CONFIG_VALUE_0") == ""
        assert env_used.get("GIT_TERMINAL_PROMPT") == "0"

    def test_generic_host_explicit_https_strict_no_ssh_fallback(self):
        """Explicit https:// URL no longer silently falls back to SSH (issue #661)."""
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://bitbucket.org/acme/rules.git")

        # All clone attempts fail; verify only HTTPS is attempted (no SSH).
        calls = self._run_clone(dl, dep, succeed_on=99)
        for call in calls:
            url = call[0][0]
            assert "git@" not in url, f"explicit https:// must not fall back to SSH, got {url}"
            assert not url.startswith("ssh://"), (
                f"explicit https:// must not fall back to ssh://, got {url}"
            )

    def test_generic_host_legacy_chain_with_allow_fallback(self):
        """APM_ALLOW_PROTOCOL_FALLBACK=1 restores cross-protocol fallback so
        clones that succeeded under the legacy chain still succeed."""
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        # Force the downloader to permit fallback regardless of env state.
        dl._allow_fallback = True
        dep = _dep("https://gitlab.com/acme/rules.git")

        # All clone attempts fail; verify both HTTPS and SSH were attempted.
        calls = self._run_clone(dl, dep, succeed_on=99)
        urls = [c[0][0] for c in calls]
        has_https = any(u.startswith("https://") for u in urls)
        has_ssh = any("git@" in u or u.startswith("ssh://") for u in urls)
        assert has_https and has_ssh, (
            f"allow_fallback should attempt both HTTPS and SSH, got urls={urls}"
        )

    def test_github_host_with_token_tries_method1_first(self):
        """GitHub with a token still tries anonymous HTTPS first."""
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://github.com/owner/repo.git")

        calls = self._run_clone(dl, dep, succeed_on=1)
        first_url = calls[0][0][0]
        assert "ghp_TESTTOKEN" not in first_url
        assert _url_host(first_url) == "github.com"

    def test_insecure_http_dep_is_strict_by_default(self):
        """HTTP deps stay on HTTP unless protocol fallback is enabled."""
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("http://gitlab.company.internal/acme/rules.git")

        dl.auth_resolver._cache.clear()
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_TESTTOKEN"}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
            patch("apm_cli.deps.github_downloader.Repo") as MockRepo,
        ):
            MockRepo.clone_from.side_effect = GitCommandError("clone", "failed")
            target = Path(tempfile.mkdtemp())
            try:
                with pytest.raises(RuntimeError, match="via insecure HTTP"):
                    dl._clone_with_fallback(dep.repo_url, target, dep_ref=dep)
            finally:
                import shutil

                shutil.rmtree(target, ignore_errors=True)

            assert MockRepo.clone_from.call_count == 1
            first_url = MockRepo.clone_from.call_args_list[0][0][0]
            env_used = MockRepo.clone_from.call_args_list[0][1]["env"]
            assert first_url == "http://gitlab.company.internal/acme/rules.git"
            assert env_used.get("GIT_ASKPASS") == "echo"
            assert env_used.get("GIT_CONFIG_GLOBAL") == dl.git_env["GIT_CONFIG_GLOBAL"]
            assert env_used.get("GIT_CONFIG_NOSYSTEM") == "1"
            assert env_used.get("GIT_CONFIG_COUNT") == "1"
            assert env_used.get("GIT_CONFIG_KEY_0") == "credential.helper"
            assert env_used.get("GIT_CONFIG_VALUE_0") == ""
            assert env_used.get("GIT_TERMINAL_PROMPT") == "0"

    def test_insecure_http_dep_with_allow_fallback_tries_http_then_ssh(self):
        """HTTP deps keep HTTP first, then may retry over SSH when enabled."""
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dl._allow_fallback = True
        dep = _dep("http://gitlab.company.internal/acme/rules.git")

        dl.auth_resolver._cache.clear()
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_TESTTOKEN"}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
            patch("apm_cli.deps.github_downloader.Repo") as MockRepo,
        ):
            MockRepo.clone_from.side_effect = [
                GitCommandError("clone", "http failed"),
                GitCommandError("clone", "ssh failed"),
            ]
            target = Path(tempfile.mkdtemp())
            try:
                with pytest.raises(RuntimeError):
                    dl._clone_with_fallback(dep.repo_url, target, dep_ref=dep)
            finally:
                import shutil

                shutil.rmtree(target, ignore_errors=True)

            urls = [c[0][0] for c in MockRepo.clone_from.call_args_list]
            envs = [c[1]["env"] for c in MockRepo.clone_from.call_args_list]

            assert urls == [
                "http://gitlab.company.internal/acme/rules.git",
                "git@gitlab.company.internal:acme/rules.git",
            ]
            assert "ghp_TESTTOKEN" not in urls[0]
            assert envs[0].get("GIT_ASKPASS") == "echo"
            assert envs[0].get("GIT_CONFIG_GLOBAL") == dl.git_env["GIT_CONFIG_GLOBAL"]
            assert envs[0].get("GIT_CONFIG_NOSYSTEM") == "1"
            assert envs[0].get("GIT_CONFIG_COUNT") == "1"
            assert envs[0].get("GIT_CONFIG_KEY_0") == "credential.helper"
            assert envs[0].get("GIT_CONFIG_VALUE_0") == ""
            assert "GIT_ASKPASS" not in envs[1]
            assert "GIT_CONFIG_GLOBAL" not in envs[1]
            assert "GIT_CONFIG_NOSYSTEM" not in envs[1]

    def test_gitlab_host_with_token_uses_header_https_first(self):
        """GitLab PAT authentication stays out of the clone URL."""
        dl = _make_downloader(gitlab_token="glpat_GITLABCLONE")
        dep = _dep("https://gitlab.com/acme/rules.git")

        calls = self._run_clone(dl, dep, succeed_on=1)
        first_url = calls[0][0][0]
        parsed_url = urlparse(first_url)
        assert parsed_url.username is None
        assert parsed_url.password is None
        assert parsed_url.hostname == "gitlab.com"
        first_env = calls[0][1]["env"]
        assert any(
            value.startswith("Authorization: Basic ")
            for key, value in first_env.items()
            if key.startswith("GIT_CONFIG_VALUE_")
        )

    def test_generic_host_error_message_mentions_credential_helpers(self):
        """When all methods fail for a generic host, the error suggests credential helpers."""
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://bitbucket.org/acme/rules.git")

        dl.auth_resolver._cache.clear()
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_TESTTOKEN"}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
            patch("apm_cli.deps.github_downloader.Repo") as MockRepo,
        ):
            MockRepo.clone_from.side_effect = GitCommandError("clone", "failed")
            target = Path(tempfile.mkdtemp())
            try:
                with pytest.raises(RuntimeError, match="credential helper"):
                    dl._clone_with_fallback(dep.repo_url, target, dep_ref=dep)
            finally:
                import shutil

                shutil.rmtree(target, ignore_errors=True)

    def test_clone_env_includes_ssh_connect_timeout(self):
        """Both locked-down and relaxed clone envs should carry GIT_SSH_COMMAND."""
        dl = _make_downloader(github_token="ghp_TESTTOKEN")

        # Locked-down env (git_env) used when token is present
        assert "GIT_SSH_COMMAND" in dl.git_env
        assert "ConnectTimeout" in dl.git_env["GIT_SSH_COMMAND"]

        # Relaxed env built for no-token paths keeps GIT_SSH_COMMAND
        relaxed = {
            k: v
            for k, v in dl.git_env.items()
            if k not in ("GIT_ASKPASS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")
        }
        assert "GIT_SSH_COMMAND" in relaxed
        assert "ConnectTimeout" in relaxed["GIT_SSH_COMMAND"]

    def test_allow_fallback_env_is_per_attempt_not_per_dep(self):
        """A non-auth HTTPS failure stays anonymous before SSH fallback.

        The arbitrary ``GitCommandError("failed")`` is not an authentication
        signal, so the configured PAT must not be resolved or presented.
        """
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dl._allow_fallback = True
        dep = _dep("owner/repo")

        mock_repo = Mock()
        mock_repo.head.commit.hexsha = "abc123"
        # Fail every attempt so we capture envs from all of them.
        effects = [GitCommandError("clone", "failed")] * 5

        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_TESTTOKEN"}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
            patch("apm_cli.deps.github_downloader.Repo") as MockRepo,
        ):
            MockRepo.clone_from.side_effect = effects
            dl.auth_resolver._cache.clear()
            target = Path(tempfile.mkdtemp())
            try:
                with pytest.raises(RuntimeError):
                    dl._clone_with_fallback(dep.repo_url, target, dep_ref=dep)
            finally:
                import shutil

                shutil.rmtree(target, ignore_errors=True)
            calls = MockRepo.clone_from.call_args_list

        # Plain HTTPS is credential-fenced; SSH retains its existing relaxed env.
        assert len(calls) >= 2, f"expected mixed chain, got {len(calls)} attempts"
        seen_auth_https = False
        seen_plain_https = False
        seen_ssh = False
        for c in calls:
            url = c[0][0]
            env_used = c[1].get("env", {})
            if url.startswith("git@") or url.startswith("ssh://"):
                seen_ssh = True
                assert "GIT_ASKPASS" not in env_used, (
                    f"SSH URL must run with relaxed env; got env={env_used}"
                )
                assert "GIT_CONFIG_GLOBAL" not in env_used
            elif "ghp_TESTTOKEN" in url:
                seen_auth_https = True
                assert env_used.get("GIT_ASKPASS") == "echo", (
                    f"token URL must run with locked-down env; got env={env_used}"
                )
            else:
                # Public GitHub HTTPS is anonymous and credential-fenced.
                seen_plain_https = True
                assert url.startswith("https://"), url
                assert env_used.get("GIT_ASKPASS") == "echo", (
                    f"plain HTTPS must run with a helper fence; got env={env_used}"
                )
        assert not seen_auth_https
        assert seen_ssh and seen_plain_https


# ===========================================================================
# Regression: ssh:// URLs with custom ports (issues #661, #731)
# ===========================================================================


class TestCloneWithFallbackPortPreservation:
    """Verify that custom SSH/HTTPS ports are preserved across all clone attempts.

    Regression for #661 and #731: Bitbucket Datacenter and self-hosted GitLab
    can serve git over non-default ports (e.g. SSH on 7999, HTTPS on 8443).
    The fix threads DependencyReference.port through _build_repo_url to the
    SSH and HTTPS URL builders so the port is never silently dropped.
    """

    def _run_clone_capture_urls(self, dep, clone_fails=True, allow_fallback=False):
        """Run _clone_with_fallback and return every URL passed to clone_from.

        When clone_fails is True, all clone attempts raise GitCommandError,
        letting the fallback chain (when permitted) exercise both SSH and
        HTTPS attempts so we can capture every emitted URL.
        """
        dl = _make_downloader()
        dl.auth_resolver._cache.clear()
        if allow_fallback:
            dl._allow_fallback = True

        called_urls = []

        def _fake_clone(url, *a, **kw):
            called_urls.append(url)
            if clone_fails:
                raise GitCommandError("clone", "failed")
            mock_repo = Mock()
            mock_repo.head.commit.hexsha = "abc123"
            return mock_repo

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
            patch("apm_cli.deps.github_downloader.Repo") as MockRepo,
        ):
            MockRepo.clone_from.side_effect = _fake_clone
            target = Path(tempfile.mkdtemp())
            try:
                dl._clone_with_fallback(dep.repo_url, target, dep_ref=dep)
            except (RuntimeError, GitCommandError):
                pass
            finally:
                import shutil

                shutil.rmtree(target, ignore_errors=True)
        return called_urls

    def test_ssh_attempt_uses_port_when_dep_ref_has_port(self):
        """Method 2 (SSH) must emit ssh://host:7999/... when dep_ref.port=7999."""
        dep = _dep("ssh://git@bitbucket.example.com:7999/project/repo.git")
        assert dep.port == 7999, "port not captured from ssh:// URL"

        urls = self._run_clone_capture_urls(dep)
        ssh_urls = [u for u in urls if u.startswith("ssh://")]
        assert ssh_urls, f"no ssh:// URL attempted, got: {urls!r}"
        assert ssh_urls[0] == "ssh://git@bitbucket.example.com:7999/project/repo.git", (
            f"SSH URL should include port 7999, got: {ssh_urls[0]!r}"
        )

    def test_https_attempt_preserves_same_port_across_protocols(self):
        """Under allow_fallback (legacy chain), HTTPS attempt must emit https://host:7999/... —
        same port as SSH. Port preservation across protocols must hold whenever the
        chain runs, even though strict default never reaches the HTTPS attempt."""
        dep = _dep("ssh://git@bitbucket.example.com:7999/project/repo.git")

        urls = self._run_clone_capture_urls(dep, allow_fallback=True)
        https_urls = [u for u in urls if u.startswith("https://")]
        assert https_urls, f"no https:// fallback attempted, got: {urls!r}"
        parsed = urlparse(https_urls[0])
        assert parsed.hostname == "bitbucket.example.com", f"HTTPS host mismatch: {https_urls[0]!r}"
        assert parsed.port == 7999, f"HTTPS URL should preserve port 7999, got: {https_urls[0]!r}"

    def test_ssh_no_port_keeps_scp_shorthand(self):
        """Without a port, SSH builder uses scp shorthand (git@host:path)."""
        dep = _dep("ssh://git@github.com/org/repo.git")
        assert dep.port is None

        urls = self._run_clone_capture_urls(dep)
        ssh_urls = [u for u in urls if "git@" in u and not u.startswith("https://")]
        assert ssh_urls, f"no SSH URL attempted, got: {urls!r}"
        # scp shorthand carries no port; ssh:// form must not inject a default port.
        if ssh_urls[0].startswith("ssh://"):
            assert urlparse(ssh_urls[0]).port is None, (
                f"ssh:// URL must not carry a default port, got: {ssh_urls[0]!r}"
            )
        else:
            # SCP shorthand `git@host:path` — path segment must not be purely numeric.
            path_segment = ssh_urls[0].split(":", 1)[1].split("/", 1)[0]
            assert not path_segment.isdigit(), (
                f"SCP shorthand must not leak a port into the path: {ssh_urls[0]!r}"
            )

    def test_https_url_with_custom_port_preserved_through_fallback(self):
        """https:// URL with port must survive through to the HTTPS clone attempt."""
        dep = _dep("https://git.company.internal:8443/team/repo.git")
        assert dep.port == 8443

        urls = self._run_clone_capture_urls(dep)
        https_urls = [u for u in urls if u.startswith("https://")]
        assert https_urls, f"no HTTPS URL attempted, got: {urls!r}"
        parsed = urlparse(https_urls[0])
        assert parsed.hostname == "git.company.internal", f"HTTPS host mismatch: {https_urls[0]!r}"
        assert parsed.port == 8443, f"HTTPS URL should preserve port 8443, got: {https_urls[0]!r}"


# ===========================================================================
# Object-style dependency entries (parse_from_dict)
# ===========================================================================


# ===========================================================================
# from_apm_yml - mixed string + dict dependencies
# ===========================================================================


class TestFromApmYmlMixedDeps:
    """Test APMPackage.from_apm_yml with both string and object-style deps."""

    def _write_yml(self, tmp_path, content):
        """Write an apm.yml file and return its Path."""
        yml_file = tmp_path / "apm.yml"
        yml_file.write_text(content, encoding="utf-8")
        return yml_file

    def test_string_only_deps(self, tmp_path):
        yml = self._write_yml(
            tmp_path,
            """
name: test-pkg
version: 1.0.0
dependencies:
  apm:
    - owner/repo
    - gitlab.com/acme/rules
""",
        )
        pkg = APMPackage.from_apm_yml(yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 2
        assert deps[0].repo_url == "owner/repo"
        assert deps[1].host == "gitlab.com"

    def test_dict_only_deps(self, tmp_path):
        yml = self._write_yml(
            tmp_path,
            """
name: test-pkg
version: 1.0.0
dependencies:
  apm:
    - git: https://gitlab.com/acme/rules.git
      path: instructions/security
      ref: v2.0
""",
        )
        pkg = APMPackage.from_apm_yml(yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        assert deps[0].host == "gitlab.com"
        assert deps[0].virtual_path == "instructions/security"
        assert deps[0].reference == "v2.0"

    def test_mixed_string_and_dict_deps(self, tmp_path):
        yml = self._write_yml(
            tmp_path,
            """
name: test-pkg
version: 1.0.0
dependencies:
  apm:
    - owner/repo
    - git: https://gitlab.com/acme/rules.git
      path: prompts/review.prompt.md
    - bitbucket.org/team/standards
""",
        )
        pkg = APMPackage.from_apm_yml(yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 3
        assert deps[0].repo_url == "owner/repo"
        assert deps[1].host == "gitlab.com"
        assert deps[1].virtual_path == "prompts/review.prompt.md"
        assert deps[2].host == "bitbucket.org"

    def test_invalid_dict_dep_raises(self, tmp_path):
        yml = self._write_yml(
            tmp_path,
            """
name: test-pkg
version: 1.0.0
dependencies:
  apm:
    - path: foo/bar
""",
        )
        with pytest.raises(ValueError, match=r"'git' field|local filesystem path"):
            APMPackage.from_apm_yml(yml)


# ===========================================================================
# Dict-style duplicate detection in _validate_and_add_packages_to_apm_yml
# ===========================================================================


class TestDictIdentityDuplicateDetection:
    """Verify that dict-style deps with 'path' get distinct identities.

    Bug: previously, dict entries used only dep_entry.get("git", ""),
    dropping 'path', so {git: "owner/repo", path: "sub"} had the same
    identity as plain "owner/repo", causing false duplicate detection.
    """

    def _write_yml(self, tmp_path, content):
        yml_file = tmp_path / "apm.yml"
        yml_file.write_text(content, encoding="utf-8")
        return yml_file

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    def test_dict_dep_with_path_not_duplicate_of_base(self, mock_validate, tmp_path):
        """A dict dep {git: X, path: Y} should not block adding the base repo X."""
        self._write_yml(
            tmp_path,
            """
name: test
version: 1.0.0
dependencies:
  apm:
    - git: https://gitlab.com/acme/rules.git
      path: instructions/security
""",
        )
        with patch("apm_cli.commands.install.Path") as MockPath:
            # Make Path("apm.yml") return our test file
            MockPath.return_value.exists.return_value = True
            # We test the identity-building logic directly
            from apm_cli.models.apm_package import DependencyReference

            # Dict dep with path
            dict_dep = {"git": "https://gitlab.com/acme/rules.git", "path": "instructions/security"}
            ref_dict = DependencyReference.parse_from_dict(dict_dep)

            # Base repo (no path)
            ref_base = DependencyReference.parse("gitlab.com/acme/rules")

            # They MUST have different identities
            assert ref_dict.get_identity() != ref_base.get_identity()

    def test_two_dict_deps_same_repo_different_paths_distinct(self):
        """Two dict deps from same repo but different paths have distinct identities."""
        from apm_cli.models.apm_package import DependencyReference

        dep1 = DependencyReference.parse_from_dict(
            {
                "git": "https://gitlab.com/acme/rules.git",
                "path": "instructions/security",
            }
        )
        dep2 = DependencyReference.parse_from_dict(
            {
                "git": "https://gitlab.com/acme/rules.git",
                "path": "prompts/review.prompt.md",
            }
        )
        assert dep1.get_identity() != dep2.get_identity()

    def test_dict_dep_no_path_same_identity_as_string(self):
        """A dict dep without path has the same identity as the string form."""
        from apm_cli.models.apm_package import DependencyReference

        dep_dict = DependencyReference.parse_from_dict(
            {
                "git": "https://gitlab.com/acme/rules.git",
            }
        )
        dep_str = DependencyReference.parse("gitlab.com/acme/rules")
        assert dep_dict.get_identity() == dep_str.get_identity()


# ===========================================================================
# _validate_package_exists env scoping for generic hosts
# ===========================================================================


class TestValidatePackageExistsEnv:
    """Verify _validate_package_exists uses the right env for generic hosts.

    Bug: previously, all non-GitHub.com hosts got the locked-down git_env
    (GIT_ASKPASS=echo, etc.), blocking credential helpers for generic hosts
    like GitLab. Explicit HTTP probes also need to keep git config isolation so
    git cannot rewrite them to SSH before the first transport attempt.
    """

    @patch(
        "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
        return_value=None,
    )
    @patch("subprocess.run")
    @patch.dict(os.environ, {}, clear=True)
    def test_generic_host_validation_snapshots_credential_helpers(
        self,
        mock_run,
        _mock_cred,
    ):
        """Generic HTTPS keeps helpers in an immutable Git config snapshot."""
        from apm_cli.commands.install import _validate_package_exists

        mock_run.return_value = Mock(returncode=0)
        config_result = subprocess.CompletedProcess(
            ["git", "config"],
            0,
            stdout=b"global\0credential.helper\nfixture-helper\0",
            stderr=b"",
        )
        with patch(
            "apm_cli.utils.git_env._git_config_run",
            return_value=config_result,
        ):
            _validate_package_exists("git.example.com/acme/rules")

        # Verify subprocess.run was called
        assert mock_run.called
        call_kwargs = mock_run.call_args
        env_used = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env", {})

        # GIT_ASKPASS must NOT be set to 'echo' (that blocks credential helpers)
        assert env_used.get("GIT_ASKPASS") != "echo", (
            "Generic host validation should not set GIT_ASKPASS=echo"
        )
        assert env_used.get("GIT_CONFIG_NOSYSTEM") == "1"
        assert env_used.get("GIT_CONFIG_GLOBAL") == os.devnull
        entries = {
            (
                env_used.get(f"GIT_CONFIG_KEY_{index}", ""),
                env_used.get(f"GIT_CONFIG_VALUE_{index}", ""),
            )
            for index in range(int(env_used.get("GIT_CONFIG_COUNT", "0")))
        }
        assert ("credential.helper", "fixture-helper") in entries
        # GIT_TERMINAL_PROMPT should still be '0' (no interactive prompts)
        assert env_used.get("GIT_TERMINAL_PROMPT") == "0"


# ===========================================================================
# GitLab direct host/path shorthand (repo-boundary probing)
# ===========================================================================


# ===========================================================================
# is_github classification edge cases
# ===========================================================================


# ===========================================================================
# _try_sparse_checkout -- per-dep token resolution
# ===========================================================================


class TestSparseCheckoutTokenResolution:
    """Verify sparse checkout follows public GitHub anonymous-first auth."""

    @pytest.mark.windows_compat
    def test_sparse_checkout_starts_anonymous_before_per_org_token(self, tmp_path):
        """Sparse checkout does not present either configured token initially."""
        org_token = "ghp_ORG_SPECIFIC"
        global_token = "ghp_GLOBAL"

        with (
            patch.dict(
                os.environ,
                {
                    "GITHUB_APM_PAT": global_token,
                    "GITHUB_APM_PAT_ACME": org_token,
                },
                clear=True,
            ),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
        ):
            dl = GitHubPackageDownloader()
            dep = _dep("acme/mono-repo/subdir")

            # Patch subprocess.run to capture the URL used in 'git remote add'
            captured_urls = []

            def capture_run(cmd, **kwargs):
                if (
                    len(cmd) >= 5
                    and cmd[0] == get_git_executable()
                    and cmd[1:4] == ["remote", "add", "origin"]
                ):
                    captured_urls.append(cmd[4])  # The URL argument (after 'origin')
                    # Fail after capturing to keep the test fast
                    return MagicMock(returncode=1, stderr="test abort")
                # Let other commands (git init, etc.) succeed
                return MagicMock(returncode=0, stderr="")

            with patch("subprocess.run", side_effect=capture_run):
                dl._try_sparse_checkout(dep, tmp_path / "sparse", "subdir", ref="main")

            assert len(captured_urls) == 1, f"Expected 1 URL capture, got {captured_urls}"
            assert org_token not in captured_urls[0]
            assert global_token not in captured_urls[0]
