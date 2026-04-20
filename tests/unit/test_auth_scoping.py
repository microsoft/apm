"""Unit tests for auth scoping: tokens are only sent to their matching hosts.

Tests cover:
- _build_repo_url: GitHub tokens only go to GitHub hosts, not to generic hosts
- _clone_with_fallback: generic hosts get relaxed env (no GIT_ASKPASS etc.)
- Object-style dependency entries (parse_from_dict, from_apm_yml)
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from urllib.parse import urlparse

import pytest
from git.exc import GitCommandError

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import DependencyReference, APMPackage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_downloader(github_token=None, ado_token=None):
    """Create a GitHubPackageDownloader with controlled tokens."""
    with patch.dict(os.environ, {
        **({"GITHUB_APM_PAT": github_token} if github_token else {}),
        **({"ADO_APM_PAT": ado_token} if ado_token else {}),
    }, clear=True), patch(
        "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
        return_value=None,
    ):
        return GitHubPackageDownloader()


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
# _build_repo_url – token scoping
# ===========================================================================

class TestBuildRepoUrlTokenScoping:
    """Verify _build_repo_url sends GitHub tokens only to GitHub hosts."""

    def test_github_com_gets_token(self):
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://github.com/owner/repo.git")
        url = dl._build_repo_url("owner/repo", use_ssh=False, dep_ref=dep)
        assert "ghp_TESTTOKEN" in url
        assert _url_host(url) == "github.com"

    def test_ghe_host_gets_token(self):
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://company.ghe.com/owner/repo.git")
        url = dl._build_repo_url("owner/repo", use_ssh=False, dep_ref=dep)
        assert "ghp_TESTTOKEN" in url
        assert _url_host(url) == "company.ghe.com"

    def test_gitlab_does_not_get_github_token(self):
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://gitlab.com/acme/rules.git")
        url = dl._build_repo_url("acme/rules", use_ssh=False, dep_ref=dep)
        assert "ghp_TESTTOKEN" not in url
        assert _url_host(url) == "gitlab.com"

    def test_bitbucket_does_not_get_github_token(self):
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://bitbucket.org/team/standards.git")
        url = dl._build_repo_url("team/standards", use_ssh=False, dep_ref=dep)
        assert "ghp_TESTTOKEN" not in url
        assert _url_host(url) == "bitbucket.org"

    def test_self_hosted_does_not_get_github_token(self):
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://git.company.internal/team/rules.git")
        url = dl._build_repo_url("team/rules", use_ssh=False, dep_ref=dep)
        assert "ghp_TESTTOKEN" not in url

    def test_ssh_url_never_embeds_token(self):
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("git@gitlab.com:acme/rules.git")
        url = dl._build_repo_url("acme/rules", use_ssh=True, dep_ref=dep)
        assert "ghp_TESTTOKEN" not in url
        assert _url_host(url) == "gitlab.com"

    def test_github_ssh_also_no_embedded_token(self):
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("git@github.com:owner/repo.git")
        url = dl._build_repo_url("owner/repo", use_ssh=True, dep_ref=dep)
        assert "ghp_TESTTOKEN" not in url

    def test_no_token_at_all_plain_url(self):
        dl = _make_downloader()
        dep = _dep("https://github.com/owner/repo.git")
        url = dl._build_repo_url("owner/repo", use_ssh=False, dep_ref=dep)
        assert "@" not in url  # no token embedded


# ===========================================================================
# _clone_with_fallback – env relaxation for generic hosts
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

        # Clear the resolver cache so resolve_for_dep re-resolves with the
        # controlled env rather than returning stale entries.
        dl.auth_resolver._cache.clear()

        with patch.dict(os.environ, env_vars, clear=True), \
             patch(
                 "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                 return_value=None,
             ), \
             patch('apm_cli.deps.github_downloader.Repo') as MockRepo:
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
        """For GitLab/Bitbucket without token, GIT_ASKPASS / GIT_CONFIG_GLOBAL are NOT set."""
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://gitlab.com/acme/rules.git")

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
        """For GitHub hosts WITHOUT a token, env is relaxed so credential helpers work."""
        dl = _make_downloader(github_token=None)
        dep = _dep("https://github.com/owner/repo.git")

        calls = self._run_clone(dl, dep, succeed_on=1)
        assert len(calls) >= 1

        env_used = calls[0][1].get("env", calls[0].kwargs.get("env"))
        assert "GIT_ASKPASS" not in env_used
        assert "GIT_CONFIG_GLOBAL" not in env_used
        assert "GIT_CONFIG_NOSYSTEM" not in env_used
        assert env_used.get("GIT_TERMINAL_PROMPT") == "0"

    def test_generic_host_explicit_https_strict_no_ssh_fallback(self):
        """Explicit https:// URL no longer silently falls back to SSH (issue #661)."""
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://gitlab.com/acme/rules.git")

        # All clone attempts fail; verify only HTTPS is attempted (no SSH).
        calls = self._run_clone(dl, dep, succeed_on=99)
        for call in calls:
            url = call[0][0]
            assert "git@" not in url, f"explicit https:// must not fall back to SSH, got {url}"
            assert not url.startswith("ssh://"), f"explicit https:// must not fall back to ssh://, got {url}"

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
        """GitHub with a token → Method 1 (auth HTTPS) is tried first."""
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://github.com/owner/repo.git")

        calls = self._run_clone(dl, dep, succeed_on=1)
        first_url = calls[0][0][0]
        assert "ghp_TESTTOKEN" in first_url
        assert _url_host(first_url) == "github.com"

    def test_generic_host_error_message_mentions_credential_helpers(self):
        """When all methods fail for a generic host, the error suggests credential helpers."""
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://gitlab.com/acme/rules.git")

        dl.auth_resolver._cache.clear()
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_TESTTOKEN"}, clear=True), \
             patch(
                 "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                 return_value=None,
             ), \
             patch('apm_cli.deps.github_downloader.Repo') as MockRepo:
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
        relaxed = {k: v for k, v in dl.git_env.items()
                   if k not in ("GIT_ASKPASS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        assert "GIT_SSH_COMMAND" in relaxed
        assert "ConnectTimeout" in relaxed["GIT_SSH_COMMAND"]

    def test_allow_fallback_env_is_per_attempt_not_per_dep(self):
        """In a mixed allow_fallback plan, only token-bearing attempts get the
        locked-down env; SSH and plain-HTTPS attempts get the relaxed env so
        user credential helpers (gh auth, Keychain, ssh-agent) keep working.

        Regression for the Wave 2 panel finding: previously the env was decided
        once per dependency from `has_token`, so SSH attempts in a token-having
        dep ran with `GIT_ASKPASS=echo` and `GIT_CONFIG_GLOBAL=/dev/null`,
        breaking encrypted-key prompts and credential helpers.
        """
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dl._allow_fallback = True
        dep = _dep("owner/repo")

        mock_repo = Mock()
        mock_repo.head.commit.hexsha = "abc123"
        # Fail every attempt so we capture envs from all of them.
        effects = [GitCommandError("clone", "failed")] * 5

        with patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_TESTTOKEN"}, clear=True), \
             patch("apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                   return_value=None), \
             patch('apm_cli.deps.github_downloader.Repo') as MockRepo:
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

        # Map URL -> env used. Token-bearing attempts must get locked-down env;
        # SSH attempts and plain-HTTPS must get the relaxed env. Plain-HTTPS
        # must NOT embed the token in the URL.
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
                # Plain HTTPS attempt: must NOT embed the token, and must run
                # with relaxed env so credential helpers (gh auth, Keychain)
                # can supply credentials.
                seen_plain_https = True
                assert url.startswith("https://"), url
                assert "GIT_ASKPASS" not in env_used, (
                    f"plain HTTPS must run with relaxed env; got env={env_used}"
                )
        assert seen_auth_https and seen_ssh and seen_plain_https, (
            "expected at least one of each attempt kind in the mixed chain"
        )


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

        with patch.dict(os.environ, {}, clear=True), \
             patch(
                 "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                 return_value=None,
             ), \
             patch('apm_cli.deps.github_downloader.Repo') as MockRepo:
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
        assert parsed.hostname == "bitbucket.example.com", (
            f"HTTPS host mismatch: {https_urls[0]!r}"
        )
        assert parsed.port == 7999, (
            f"HTTPS URL should preserve port 7999, got: {https_urls[0]!r}"
        )

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
        assert parsed.hostname == "git.company.internal", (
            f"HTTPS host mismatch: {https_urls[0]!r}"
        )
        assert parsed.port == 8443, (
            f"HTTPS URL should preserve port 8443, got: {https_urls[0]!r}"
        )


# ===========================================================================
# Object-style dependency entries (parse_from_dict)
# ===========================================================================

class TestParseFromDict:
    """Test DependencyReference.parse_from_dict for object-style entries."""

    def test_basic_git_url(self):
        dep = DependencyReference.parse_from_dict({"git": "https://gitlab.com/acme/rules.git"})
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/rules"
        assert dep.virtual_path is None
        assert dep.reference is None

    def test_git_url_with_path(self):
        dep = DependencyReference.parse_from_dict({
            "git": "https://gitlab.com/acme/rules.git",
            "path": "instructions/security",
        })
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/rules"
        assert dep.virtual_path == "instructions/security"
        assert dep.is_virtual is True

    def test_git_url_with_ref(self):
        dep = DependencyReference.parse_from_dict({
            "git": "https://bitbucket.org/team/standards.git",
            "ref": "v2.0",
        })
        assert dep.host == "bitbucket.org"
        assert dep.reference == "v2.0"

    def test_git_url_with_alias(self):
        dep = DependencyReference.parse_from_dict({
            "git": "git@gitlab.com:acme/rules.git",
            "alias": "my-rules",
        })
        assert dep.alias == "my-rules"
        assert dep.host == "gitlab.com"

    def test_git_url_with_all_fields(self):
        dep = DependencyReference.parse_from_dict({
            "git": "https://gitlab.com/acme/rules.git",
            "path": "prompts/review.prompt.md",
            "ref": "main",
            "alias": "review",
        })
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/rules"
        assert dep.virtual_path == "prompts/review.prompt.md"
        assert dep.is_virtual is True
        assert dep.reference == "main"
        assert dep.alias == "review"

    def test_ssh_git_url(self):
        dep = DependencyReference.parse_from_dict({
            "git": "git@bitbucket.org:team/rules.git",
            "path": "security",
        })
        assert dep.host == "bitbucket.org"
        assert dep.repo_url == "team/rules"
        assert dep.virtual_path == "security"

    def test_path_strips_slashes(self):
        dep = DependencyReference.parse_from_dict({
            "git": "https://gitlab.com/acme/rules.git",
            "path": "/prompts/file.md/",
        })
        assert dep.virtual_path == "prompts/file.md"

    def test_ref_in_url_overridden_by_field(self):
        """'ref' field takes precedence over inline #ref in git URL."""
        dep = DependencyReference.parse_from_dict({
            "git": "https://gitlab.com/acme/rules.git#v1.0",
            "ref": "v2.0",
        })
        assert dep.reference == "v2.0"

    # --- Error cases ---

    def test_missing_git_field(self):
        # With local path support, {"path": "foo"} is treated as a local path attempt.
        # Since "foo" is not a valid local or remote dependency, it raises ValueError.
        with pytest.raises(ValueError):
            DependencyReference.parse_from_dict({"path": "foo"})

    def test_empty_git_field(self):
        with pytest.raises(ValueError, match="non-empty string"):
            DependencyReference.parse_from_dict({"git": ""})

    def test_git_field_not_string(self):
        with pytest.raises(ValueError, match="non-empty string"):
            DependencyReference.parse_from_dict({"git": 42})

    def test_empty_path_field(self):
        with pytest.raises(ValueError, match="'path' field"):
            DependencyReference.parse_from_dict({"git": "https://gitlab.com/a/b.git", "path": ""})

    def test_empty_ref_field(self):
        with pytest.raises(ValueError, match="'ref' field"):
            DependencyReference.parse_from_dict({"git": "https://gitlab.com/a/b.git", "ref": ""})

    def test_empty_alias_field(self):
        with pytest.raises(ValueError, match="'alias' field"):
            DependencyReference.parse_from_dict({"git": "https://gitlab.com/a/b.git", "alias": ""})


# ===========================================================================
# from_apm_yml – mixed string + dict dependencies
# ===========================================================================

class TestFromApmYmlMixedDeps:
    """Test APMPackage.from_apm_yml with both string and object-style deps."""

    def _write_yml(self, tmp_path, content):
        """Write an apm.yml file and return its Path."""
        yml_file = tmp_path / "apm.yml"
        yml_file.write_text(content, encoding="utf-8")
        return yml_file

    def test_string_only_deps(self, tmp_path):
        yml = self._write_yml(tmp_path, """
name: test-pkg
version: 1.0.0
dependencies:
  apm:
    - owner/repo
    - gitlab.com/acme/rules
""")
        pkg = APMPackage.from_apm_yml(yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 2
        assert deps[0].repo_url == "owner/repo"
        assert deps[1].host == "gitlab.com"

    def test_dict_only_deps(self, tmp_path):
        yml = self._write_yml(tmp_path, """
name: test-pkg
version: 1.0.0
dependencies:
  apm:
    - git: https://gitlab.com/acme/rules.git
      path: instructions/security
      ref: v2.0
""")
        pkg = APMPackage.from_apm_yml(yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        assert deps[0].host == "gitlab.com"
        assert deps[0].virtual_path == "instructions/security"
        assert deps[0].reference == "v2.0"

    def test_mixed_string_and_dict_deps(self, tmp_path):
        yml = self._write_yml(tmp_path, """
name: test-pkg
version: 1.0.0
dependencies:
  apm:
    - owner/repo
    - git: https://gitlab.com/acme/rules.git
      path: prompts/review.prompt.md
    - bitbucket.org/team/standards
""")
        pkg = APMPackage.from_apm_yml(yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 3
        assert deps[0].repo_url == "owner/repo"
        assert deps[1].host == "gitlab.com"
        assert deps[1].virtual_path == "prompts/review.prompt.md"
        assert deps[2].host == "bitbucket.org"

    def test_invalid_dict_dep_raises(self, tmp_path):
        yml = self._write_yml(tmp_path, """
name: test-pkg
version: 1.0.0
dependencies:
  apm:
    - path: foo/bar
""")
        with pytest.raises(ValueError, match="'git' field|local filesystem path"):
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
        import yaml
        yml = self._write_yml(tmp_path, """
name: test
version: 1.0.0
dependencies:
  apm:
    - git: https://gitlab.com/acme/rules.git
      path: instructions/security
""")
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

        dep1 = DependencyReference.parse_from_dict({
            "git": "https://gitlab.com/acme/rules.git",
            "path": "instructions/security",
        })
        dep2 = DependencyReference.parse_from_dict({
            "git": "https://gitlab.com/acme/rules.git",
            "path": "prompts/review.prompt.md",
        })
        assert dep1.get_identity() != dep2.get_identity()

    def test_dict_dep_no_path_same_identity_as_string(self):
        """A dict dep without path has the same identity as the string form."""
        from apm_cli.models.apm_package import DependencyReference

        dep_dict = DependencyReference.parse_from_dict({
            "git": "https://gitlab.com/acme/rules.git",
        })
        dep_str = DependencyReference.parse("gitlab.com/acme/rules")
        assert dep_dict.get_identity() == dep_str.get_identity()


# ===========================================================================
# _validate_package_exists env scoping for generic hosts
# ===========================================================================

class TestValidatePackageExistsEnv:
    """Verify _validate_package_exists uses relaxed env for generic hosts.

    Bug: previously, all non-GitHub.com hosts got the locked-down git_env
    (GIT_ASKPASS=echo, etc.), blocking credential helpers for generic hosts
    like GitLab. The clone step already relaxed this, but validation didn't.
    """

    @patch("apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git", return_value=None)
    @patch("subprocess.run")
    @patch.dict(os.environ, {}, clear=True)
    def test_generic_host_validation_allows_credential_helpers(self, mock_run, _mock_cred):
        """git ls-remote for a generic host should NOT have GIT_ASKPASS=echo."""
        from apm_cli.commands.install import _validate_package_exists

        mock_run.return_value = Mock(returncode=0)
        _validate_package_exists("gitlab.com/acme/rules")

        # Verify subprocess.run was called
        assert mock_run.called
        call_kwargs = mock_run.call_args
        env_used = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env", {})

        # GIT_ASKPASS must NOT be set to 'echo' (that blocks credential helpers)
        assert env_used.get("GIT_ASKPASS") != "echo", \
            "Generic host validation should not set GIT_ASKPASS=echo"
        # GIT_CONFIG_NOSYSTEM must NOT be '1' (allows system git config)
        assert env_used.get("GIT_CONFIG_NOSYSTEM") != "1", \
            "Generic host validation should not set GIT_CONFIG_NOSYSTEM=1"
        # GIT_TERMINAL_PROMPT should still be '0' (no interactive prompts)
        assert env_used.get("GIT_TERMINAL_PROMPT") == "0"

    @patch("apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git", return_value=None)
    @patch("subprocess.run")
    @patch.dict(os.environ, {"ADO_APM_PAT": "test-ado-token"}, clear=True)
    def test_ado_host_validation_uses_locked_env(self, mock_run, _mock_cred):
        """git ls-remote for ADO should use the locked-down env (APM manages auth)."""
        from apm_cli.commands.install import _validate_package_exists

        mock_run.return_value = Mock(returncode=0)
        _validate_package_exists("dev.azure.com/myorg/myproject/myrepo")

        assert mock_run.called
        call_kwargs = mock_run.call_args
        env_used = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env", {})

        # ADO should keep the locked-down env
        assert "GIT_ASKPASS" in env_used or "GIT_CONFIG_NOSYSTEM" in env_used


# ===========================================================================
# is_github classification edge cases
# ===========================================================================

class TestIsGitHubClassification:
    """Verify is_github is correctly determined for edge-case hosts."""

    def test_empty_host_defaults_to_github(self):
        """When no host is set, packages default to GitHub behavior."""
        downloader = _make_downloader(github_token="ghp_test123")
        dep_ref = _dep("microsoft/apm-sample-package")

        # No host set → is_github should be True
        dep_host = dep_ref.host if dep_ref else None
        # Original bug: `dep_host and is_github_hostname(dep_host) or (not dep_host)`
        # With empty/None dep_host, this should return True
        from apm_cli.utils.github_host import is_github_hostname
        if dep_host:
            is_github = is_github_hostname(dep_host)
        else:
            is_github = True
        assert is_github is True

    def test_gitlab_host_is_not_github(self):
        """GitLab host should NOT be classified as GitHub."""
        dep_ref = _dep("gitlab.com/acme/rules")
        from apm_cli.utils.github_host import is_github_hostname
        assert is_github_hostname(dep_ref.host) is False

    def test_ghe_host_is_github(self):
        """GitHub Enterprise host should be classified as GitHub."""
        dep_ref = _dep("https://company.ghe.com/org/repo.git")
        from apm_cli.utils.github_host import is_github_hostname
        assert is_github_hostname(dep_ref.host) is True


# ===========================================================================
# _try_sparse_checkout -- per-dep token resolution
# ===========================================================================

class TestSparseCheckoutTokenResolution:
    """Verify _try_sparse_checkout uses resolve_for_dep() for per-dep tokens."""

    def test_sparse_checkout_uses_per_org_token(self, tmp_path):
        """Sparse checkout should use per-org token, not the global instance token."""
        org_token = "ghp_ORG_SPECIFIC"
        global_token = "ghp_GLOBAL"

        with patch.dict(os.environ, {
            "GITHUB_APM_PAT": global_token,
            "GITHUB_APM_PAT_ACME": org_token,
        }, clear=True), patch(
            "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
            return_value=None,
        ):
            dl = GitHubPackageDownloader()
            dep = _dep("acme/mono-repo/subdir")

            # Patch subprocess.run to capture the URL used in 'git remote add'
            captured_urls = []

            def capture_run(cmd, **kwargs):
                if len(cmd) >= 5 and cmd[:3] == ["git", "remote", "add"]:
                    captured_urls.append(cmd[4])  # The URL argument (after 'origin')
                    # Fail after capturing to keep the test fast
                    return MagicMock(returncode=1, stderr="test abort")
                # Let other commands (git init, etc.) succeed
                return MagicMock(returncode=0, stderr="")

            with patch("subprocess.run", side_effect=capture_run):
                dl._try_sparse_checkout(dep, tmp_path / "sparse", "subdir", ref="main")

            assert len(captured_urls) == 1, f"Expected 1 URL capture, got {captured_urls}"
            # The per-org token should be in the URL, not the global one
            assert org_token in captured_urls[0], (
                f"Expected org-specific token in sparse checkout URL, "
                f"got: {captured_urls[0]}"
            )
            assert global_token not in captured_urls[0]
