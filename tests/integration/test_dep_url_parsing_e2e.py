"""End-to-end tests for #1159 SCP/EMU + ADO v3 SSH URL parsing.

The shared regex ``SCP_LIKE_RE`` (``cache/url_normalize.py``) is
consumed by THREE call sites in the codebase:

  1. ``cache/url_normalize.py`` itself (lockfile normalization)
  2. ``policy/discovery.py::_parse_remote_url`` (audit / install
     auto-discovery)
  3. ``models/dependency/reference.py::DependencyReference._parse_ssh_url``
     (dependency parsing in apm.yml)

Unit tests (``tests/unit/cache/test_url_normalize.py``,
``tests/unit/policy/test_discovery.py``,
``tests/unit/test_canonicalization.py``) verify each consumer
independently, but no end-to-end test exercises the regex through the
full ``apm audit`` / ``apm install`` codepath with a real ``git
remote`` configured to an EMU or ADO v3 SSH URL.

These tests close that gap: they stand up a real git working tree,
configure a real ``origin`` remote with the URL form being defended,
and run the auto-discovery codepath end-to-end (mocking only the
network fetch so the test is hermetic).  A regression that bypasses
``SCP_LIKE_RE`` on any of the three consumers would surface as the
pre-#1159 silent fall-through (``no_git_remote`` outcome) instead of
the expected ``(org, host)`` extraction.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from apm_cli.policy.discovery import (
    PolicyFetchResult,
    _extract_org_from_git_remote,
    discover_policy_with_chain,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _git_init_with_remote(path: Path, remote_url: str) -> None:
    """Initialise a real git repo with a configured origin remote."""
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", remote_url],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _write_minimal_apm_yml(project: Path, deps: list[str] | None = None) -> Path:
    apm_yml = project / "apm.yml"
    body = "name: test-project\nversion: '1.0.0'\n"
    if deps:
        body += "dependencies:\n  apm:\n" + "".join(f"    - {d}\n" for d in deps)
    apm_yml.write_text(body, encoding="utf-8")
    return apm_yml


class TestExtractOrgFromRealGitRemote:
    """Auto-discovery org extraction across SCP/EMU + ADO v3 SSH forms.

    Pre-#1159: any SSH URL with a non-``git`` user (EMU,
    ``enterprise-user@ghe.corp.com:org/repo``) silently failed the
    SCP regex and returned ``None`` from
    ``_extract_org_from_git_remote``, which then surfaced as the
    silent ``no_git_remote`` fall-through.

    This test runs the REAL ``git remote get-url origin`` codepath
    against a REAL git working tree to prove the shared
    ``SCP_LIKE_RE`` is reachable end-to-end.
    """

    def test_emu_ssh_user_extracts_org(self, tmp_path):
        _git_init_with_remote(tmp_path, "enterprise-user@github.com:contoso/my-project.git")
        result = _extract_org_from_git_remote(tmp_path)
        assert result == ("contoso", "github.com")

    def test_emu_on_ghe_host_extracts_org_and_host(self, tmp_path):
        _git_init_with_remote(tmp_path, "enterprise-user@ghe.corp.com:contoso/my-project.git")
        result = _extract_org_from_git_remote(tmp_path)
        assert result == ("contoso", "ghe.corp.com")

    def test_legacy_git_user_still_extracts_org(self, tmp_path):
        """Regression guard: the SCP regex change must not break ``git@``."""
        _git_init_with_remote(tmp_path, "git@github.com:contoso/my-project.git")
        result = _extract_org_from_git_remote(tmp_path)
        assert result == ("contoso", "github.com")

    def test_ado_v3_ssh_extracts_org_not_v3(self, tmp_path):
        """ADO SSH form: the ``v3`` segment MUST NOT be parsed as the org."""
        _git_init_with_remote(
            tmp_path,
            "git@ssh.dev.azure.com:v3/myorg/myproject/myrepo",
        )
        result = _extract_org_from_git_remote(tmp_path)
        # Pre-fix returned ("v3", "ssh.dev.azure.com") -- silent
        # mis-attribution. Post-fix returns the real org.
        assert result == ("myorg", "ssh.dev.azure.com")


class TestDiscoverPolicyWithChainEMUEndToEnd:
    """Full auto-discovery pipeline with EMU SSH remote.

    Mocks only the inner ``_fetch_from_repo`` so the test is
    hermetic, but exercises the real ``git remote`` introspection
    + URL parse + repo-ref construction. A regression in any of the
    three SCP-regex consumers would cause discovery to short-circuit
    before reaching the fetch.
    """

    def test_emu_ssh_remote_routes_to_correct_org_policy_repo(self, tmp_path):
        _git_init_with_remote(tmp_path, "enterprise-user@github.com:contoso/some-app.git")
        _write_minimal_apm_yml(tmp_path)

        sentinel = PolicyFetchResult(outcome="absent", source="org:contoso/.github")

        with patch(
            "apm_cli.policy.discovery._fetch_from_repo", return_value=sentinel
        ) as mock_fetch:
            result = discover_policy_with_chain(tmp_path)

        # Discovery reached _fetch_from_repo (proves SCP_LIKE_RE
        # matched and org was extracted) and routed to contoso/.github.
        assert mock_fetch.call_count >= 1
        first_call_repo_ref = mock_fetch.call_args_list[0].args[0]
        assert first_call_repo_ref == "contoso/.github"
        assert result is sentinel

    def test_ado_v3_ssh_remote_routes_to_correct_org_policy_repo(self, tmp_path):
        _git_init_with_remote(
            tmp_path,
            "git@ssh.dev.azure.com:v3/realorg/myproject/myrepo",
        )
        _write_minimal_apm_yml(tmp_path)

        sentinel = PolicyFetchResult(outcome="absent")

        with patch(
            "apm_cli.policy.discovery._fetch_from_repo", return_value=sentinel
        ) as mock_fetch:
            result = discover_policy_with_chain(tmp_path)

        # Pre-fix the repo_ref would have been constructed from
        # org="v3" -- a silent mis-routing.  Post-fix the real org
        # ``realorg`` is used.
        assert mock_fetch.call_count >= 1
        first_call_repo_ref = mock_fetch.call_args_list[0].args[0]
        # ADO host attaches a host prefix when host != github.com.
        assert "realorg/.github" in first_call_repo_ref
        assert "v3/.github" not in first_call_repo_ref
        assert result is sentinel


class TestDependencyReferenceParsesEMUAndAdoSsh:
    """Defends the SCP regex on the dependency-parse codepath
    (``DependencyReference._parse_ssh_url``).

    Pre-#1159 the inline regex in ``models/dependency/reference.py``
    accepted only ``git@`` users.  Post-fix it uses the shared
    ``SCP_LIKE_RE`` from ``cache/url_normalize.py``.

    These tests parse through the real ``APMPackage.from_apm_yml``
    entry point (the same codepath ``apm install`` follows) to
    catch any future regression that bypasses the shared regex.
    """

    def test_emu_user_in_apm_yml_parses_through_real_package_loader(self, tmp_path):
        apm_yml = _write_minimal_apm_yml(
            tmp_path, deps=["enterprise-user@github.com:contoso/my-pkg"]
        )
        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        dep = deps[0]
        # Pre-fix this dep would have been rejected by _parse_ssh_url
        # and fallen through to the string fallback path.
        assert dep.host == "github.com"
        assert dep.repo_url == "contoso/my-pkg"

    def test_ghe_emu_user_in_apm_yml_parses_through_real_package_loader(self, tmp_path):
        apm_yml = _write_minimal_apm_yml(
            tmp_path, deps=["enterprise-user@ghe.corp.com:contoso/my-pkg"]
        )
        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        dep = deps[0]
        assert dep.host == "ghe.corp.com"
        assert dep.repo_url == "contoso/my-pkg"

    def test_legacy_git_user_in_apm_yml_still_parses(self, tmp_path):
        """Regression guard: ``git@`` SCP form must still parse cleanly."""
        apm_yml = _write_minimal_apm_yml(tmp_path, deps=["git@github.com:contoso/my-pkg"])
        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        dep = deps[0]
        assert dep.host == "github.com"
        assert dep.repo_url == "contoso/my-pkg"


class TestSshUserThreadedThroughCloneUrl:
    """Defends issue #1383 end-to-end.

    Pre-fix: writing ``myuser@github.com:org/repo`` in apm.yml resulted in
    ``apm install`` cloning ``git@github.com:org/repo.git`` (wrong user). The
    custom user was parsed by regex but discarded; the host backend always
    asked ``build_ssh_url`` for the hardcoded ``git`` user.

    Post-fix: the SSH user is captured at parse time, validated against the
    supply-chain allowlist, and threaded through to the clone URL.

    These tests parse through the real ``APMPackage.from_apm_yml`` entry
    point AND then invoke the real ``GitHubBackend.build_clone_ssh_url`` so
    a regression in either layer surfaces here.
    """

    def test_custom_scp_user_survives_through_clone_url(self, tmp_path):
        from apm_cli.core.auth import HostInfo
        from apm_cli.deps.host_backends import GitHubBackend

        apm_yml = _write_minimal_apm_yml(tmp_path, deps=["myuser@github.com:contoso/my-pkg"])
        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        dep = deps[0]
        assert dep.ssh_user == "myuser"

        host_info = HostInfo(
            host="github.com",
            kind="github",
            has_public_repos=True,
            api_base="https://api.github.com",
        )
        backend = GitHubBackend(host_info)
        clone_url = backend.build_clone_ssh_url(dep)
        assert clone_url == "myuser@github.com:contoso/my-pkg.git"

    def test_custom_ssh_protocol_user_with_port_survives_through_clone_url(self, tmp_path):
        from apm_cli.core.auth import HostInfo
        from apm_cli.deps.host_backends import GenericGitBackend

        apm_yml = _write_minimal_apm_yml(
            tmp_path, deps=["ssh://buildbot@git.corp.com:7999/team/my-pkg.git"]
        )
        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        dep = deps[0]
        assert dep.ssh_user == "buildbot"
        assert dep.port == 7999

        host_info = HostInfo(
            host="git.corp.com",
            kind="generic",
            has_public_repos=False,
            api_base="https://git.corp.com",
        )
        backend = GenericGitBackend(host_info)
        clone_url = backend.build_clone_ssh_url(dep)
        assert clone_url == "ssh://buildbot@git.corp.com:7999/team/my-pkg.git"

    def test_legacy_git_user_clone_url_unchanged_for_backward_compat(self, tmp_path):
        """Regression guard: the default ``git@`` SSH user MUST stay the
        default for any dep written without an explicit user. This is what
        every existing apm.yml in the wild relies on."""
        from apm_cli.core.auth import HostInfo
        from apm_cli.deps.host_backends import GitHubBackend

        apm_yml = _write_minimal_apm_yml(tmp_path, deps=["git@github.com:contoso/my-pkg"])
        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()
        host_info = HostInfo(
            host="github.com",
            kind="github",
            has_public_repos=True,
            api_base="https://api.github.com",
        )
        backend = GitHubBackend(host_info)
        clone_url = backend.build_clone_ssh_url(deps[0])
        assert clone_url == "git@github.com:contoso/my-pkg.git"

    def test_option_injection_user_in_apm_yml_is_rejected(self, tmp_path):
        """A malicious or typo'd apm.yml that starts the SSH user with ``-``
        (which OpenSSH would interpret as an option flag like
        ``-oProxyCommand=...``) MUST fail at parse time, not silently flow
        into ``git clone`` argv."""
        apm_yml = _write_minimal_apm_yml(tmp_path, deps=["-oProxy@github.com:contoso/my-pkg"])
        with pytest.raises(ValueError):
            APMPackage.from_apm_yml(apm_yml).get_apm_dependencies()

    def test_percent_encoded_ssh_user_is_rejected(self, tmp_path):
        """``urlparse('ssh://%2DoProxyCommand@host/repo').username`` returns
        ``-oProxyCommand`` — the percent-decode would smuggle option injection
        past the allowlist. The parser must reject percent-encoded userinfo
        BEFORE urlparse decodes it."""
        apm_yml = _write_minimal_apm_yml(
            tmp_path, deps=["ssh://%2DoProxyCommand@github.com/contoso/my-pkg.git"]
        )
        with pytest.raises(ValueError):
            APMPackage.from_apm_yml(apm_yml).get_apm_dependencies()
