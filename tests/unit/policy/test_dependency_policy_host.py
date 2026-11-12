"""Host-aware dependency allow/deny matching (#3509).

Every dependency is evaluated against its effective host: the host written in
the reference, or the default host (``GITHUB_HOST`` or ``github.com``) when
none is written. Host-qualified patterns match that host exactly; hostless
patterns keep matching any host.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency import DependencyReference
from apm_cli.models.dependency.identity import split_policy_pattern_host
from apm_cli.policy.matcher import check_dependency_allowed
from apm_cli.policy.policy_checks import (
    _check_dependency_allowlist,
    _check_dependency_denylist,
    run_dependency_policy_checks,
    run_policy_checks,
)
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

ENTERPRISE_ALLOW = (
    "ghes.corp.example/**",
    "contoso.ghe.com/**",
    "github.com/example-org/**",
)


@pytest.fixture(autouse=True)
def _default_github_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_HOST", raising=False)


def _allowed(raw: str, *, allow=None, deny=None) -> bool:
    policy = DependencyPolicy(allow=allow, deny=deny)
    allowed, _reason = check_dependency_allowed(DependencyReference.parse(raw), policy)
    return allowed


class TestDefaultHostDependencies:
    def test_hostless_dependency_matches_github_com_rule(self) -> None:
        assert _allowed("example-org/repo", allow=("github.com/example-org/**",))

    def test_hostless_dependency_rejected_by_rule_for_other_org(self) -> None:
        assert not _allowed("example-org/repo", allow=("github.com/other-org/**",))

    def test_hostless_dependency_rejected_when_default_host_unapproved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_HOST", "ghes.unapproved.example")
        dependency = DependencyReference.parse("example-org/repo")

        result = _check_dependency_allowlist(
            [dependency], DependencyPolicy(allow=("github.com/example-org/**",))
        )

        assert not result.passed
        assert result.details == [
            "ghes.unapproved.example/example-org/repo: not in allowed sources"
        ]

    def test_hostless_dependency_matches_approved_default_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_HOST", "ghes.corp.example")
        assert _allowed("example-org/repo", allow=("ghes.corp.example/**",))
        assert not _allowed("example-org/repo", allow=("github.com/**",))


class TestExplicitHostDependencies:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ghes.corp.example/team/repo", True),
            ("contoso.ghe.com/any-org/repo", True),
            ("github.com/example-org/repo", True),
            ("github.com/other-org/repo", False),
            ("gitlab.com/example-org/repo", False),
            ("ghes.other.example/team/repo", False),
        ],
    )
    def test_enterprise_policy_matrix(self, raw: str, expected: bool) -> None:
        assert _allowed(raw, allow=ENTERPRISE_ALLOW) is expected

    def test_explicit_host_matches_only_its_own_host(self) -> None:
        assert not _allowed("ghes.corp.example/example-org/repo", allow=("github.com/**",))
        assert not _allowed("github.com/team/repo", allow=("ghes.corp.example/**",))

    @pytest.mark.parametrize(
        "raw",
        [
            "ghes.corp.example.evil.test/team/repo",
            "evil.ghes.corp.example/team/repo",
            "ghes-corp.example/team/repo",
            "xghes.corp.example/team/repo",
            "corp.example/team/repo",
        ],
    )
    def test_lookalike_and_subdomain_hosts_do_not_match(self, raw: str) -> None:
        assert not _allowed(raw, allow=("ghes.corp.example/**",))

    def test_host_comparison_ignores_case(self) -> None:
        assert _allowed("GitHub.com/Example-Org/Repo", allow=("github.com/example-org/**",))
        assert _allowed("example-org/repo", allow=("GITHUB.COM/Example-Org/**",))

    def test_wildcards_are_not_expanded_in_host_segment(self) -> None:
        assert not _allowed("ghes.corp.example/team/repo", allow=("*.corp.example/**",))

    def test_host_qualified_deny_only_blocks_that_host(self) -> None:
        assert not _allowed("github.com/team/repo", deny=("github.com/**",))
        assert not _allowed("team/repo", deny=("github.com/**",))
        assert _allowed("ghes.corp.example/team/repo", deny=("github.com/**",))

    def test_deny_wins_over_host_qualified_allow(self) -> None:
        assert not _allowed(
            "github.com/example-org/repo",
            allow=("github.com/example-org/**",),
            deny=("github.com/example-org/repo",),
        )

    def test_port_does_not_change_host_identity(self) -> None:
        assert _allowed("ghes.corp.example:8443/team/repo", allow=("ghes.corp.example/**",))
        assert _allowed("ghes.corp.example/team/repo", allow=("ghes.corp.example:8443/**",))

    def test_host_qualified_patterns_never_match_local_dependencies(self) -> None:
        assert not _allowed("./packages/x", allow=("github.com/**",))

    def test_default_host_port_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_HOST", "ghes.corp.example:8443")
        assert DependencyReference.parse("team/repo").policy_host == "ghes.corp.example"
        assert _allowed("team/repo", allow=("ghes.corp.example/**",))
        assert not _allowed("team/repo", deny=("ghes.corp.example/**",))
        assert not _allowed("team/repo", allow=("github.com/**",))

    def test_trailing_dns_dot_does_not_change_host_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_HOST", "ghes.corp.example.")
        assert DependencyReference.parse("team/repo").policy_host == "ghes.corp.example"
        assert not _allowed("team/repo", deny=("ghes.corp.example/**",))
        assert _allowed("team/repo", allow=("ghes.corp.example./**",))
        assert not _allowed("team/repo", allow=("ghes.corp.example.evil/**",))


class TestReferenceFormsAreEquivalent:
    @pytest.mark.parametrize(
        "forms",
        [
            (
                "example-org/repo",
                "github.com/example-org/repo",
                "https://github.com/example-org/repo.git",
                "git@github.com:example-org/repo.git",
                "ssh://git@github.com/example-org/repo.git",
            ),
            (
                "ghes.corp.example/team/repo",
                "https://ghes.corp.example/team/repo.git",
                "git@ghes.corp.example:team/repo.git",
                "ssh://git@ghes.corp.example:7999/team/repo.git",
            ),
        ],
    )
    @pytest.mark.parametrize(
        "policy",
        [
            {"allow": ENTERPRISE_ALLOW},
            {"allow": ("github.com/other-org/**",)},
            {"deny": ("github.com/example-org/**",)},
            {"deny": ("ghes.corp.example/team/*",)},
            {"allow": ("example-org/**", "team/**")},
        ],
    )
    def test_shorthand_https_and_ssh_give_identical_results(self, forms, policy) -> None:
        results = {raw: _allowed(raw, **policy) for raw in forms}
        assert len(set(results.values())) == 1, results


class TestHostlessCompatibility:
    def test_hostless_pattern_still_matches_any_host(self) -> None:
        for raw in (
            "example-org/repo",
            "ghes.corp.example/example-org/repo",
            "gitlab.com/example-org/repo",
        ):
            assert _allowed(raw, allow=("example-org/**",)), raw

    def test_hostless_deny_still_blocks_any_host(self) -> None:
        for raw in ("evil/repo", "gitlab.com/evil/repo", "ghes.corp.example/evil/repo"):
            assert not _allowed(raw, deny=("evil/**",)), raw

    def test_recursive_glob_still_matches_everything(self) -> None:
        assert _allowed("ghes.corp.example/team/repo", allow=("**",))


class TestHostRecognitionRule:
    @pytest.mark.parametrize(
        ("pattern", "expected"),
        [
            ("example-org/**", (None, "example-org/**")),
            ("**", (None, "**")),
            ("*/repo", (None, "*/repo")),
            ("github.com/example-org/**", ("github.com", "example-org/**")),
            ("GitHub.COM/Example-Org/**", ("github.com", "Example-Org/**")),
            ("ghes.corp.example:8443/**", ("ghes.corp.example", "**")),
            ("gitlab.com/my.group/**", ("gitlab.com", "my.group/**")),
            ("my.group/**", ("my.group", "**")),
            ("github.com", (None, "github.com")),
            ("./packages/**", (None, "./packages/**")),
            ("../shared/**", (None, "../shared/**")),
            (".hidden.example/**", (None, ".hidden.example/**")),
        ],
    )
    def test_first_segment_with_a_dot_is_the_host(self, pattern, expected) -> None:
        assert split_policy_pattern_host(pattern) == expected

    @pytest.mark.parametrize(
        ("raw", "pattern"),
        [("./packages/x", "./packages/**"), ("../shared/lib", "../shared/**")],
    )
    def test_relative_local_path_patterns_stay_hostless(self, raw: str, pattern: str) -> None:
        assert _allowed(raw, allow=(pattern,))
        assert not _allowed(raw, deny=(pattern,))

    def test_dotted_gitlab_group_requires_host_qualification(self) -> None:
        assert _allowed("gitlab.com/my.group/repo", allow=("gitlab.com/my.group/**",))
        assert not _allowed("gitlab.com/my.group/repo", allow=("my.group/**",))
        assert not _allowed("gitlab.com/other.group/repo", allow=("gitlab.com/my.group/**",))


class TestTransitiveDependencies:
    def test_install_gate_checks_transitive_dependency_host(self) -> None:
        direct = DependencyReference.parse("github.com/example-org/app")
        transitive = DependencyReference.parse("ghes.unapproved.example/example-org/lib")
        policy = ApmPolicy(dependencies=DependencyPolicy(allow=ENTERPRISE_ALLOW))

        result = run_dependency_policy_checks(
            [direct, transitive],
            policy=policy,
            direct_dep_keys={direct.get_unique_key()},
        )

        allowlist = next(c for c in result.checks if c.name == "dependency-allowlist")
        assert not allowlist.passed
        assert allowlist.details == [
            "ghes.unapproved.example/example-org/lib: not in allowed sources"
        ]

    def _write_project(self, root: Path, transitive_host: str) -> None:
        (root / "apm.yml").write_text(
            textwrap.dedent(
                """\
                name: demo
                version: 1.0.0
                dependencies:
                  apm:
                    - example-org/app
                """
            ),
            encoding="utf-8",
        )
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(repo_url="example-org/app", host="github.com", depth=1)
        )
        lock.add_dependency(
            LockedDependency(
                repo_url="example-org/lib",
                host=transitive_host,
                depth=2,
                resolved_by="example-org/app",
            )
        )
        lock.write(root / "apm.lock.yaml")

    @pytest.mark.parametrize(
        ("transitive_host", "expected"),
        [("ghes.unapproved.example", False), ("ghes.corp.example", True)],
    )
    def test_audit_checks_locked_transitive_dependency_host(
        self, tmp_path: Path, transitive_host: str, expected: bool
    ) -> None:
        self._write_project(tmp_path, transitive_host)
        policy = ApmPolicy(dependencies=DependencyPolicy(allow=ENTERPRISE_ALLOW))

        result = run_policy_checks(tmp_path, policy, fail_fast=False)

        allowlist = next(c for c in result.checks if c.name == "dependency-allowlist")
        assert allowlist.passed is expected

    def test_audit_checks_locked_transitive_dependency_against_deny(self, tmp_path: Path) -> None:
        self._write_project(tmp_path, "ghes.corp.example")
        policy = ApmPolicy(dependencies=DependencyPolicy(deny=("ghes.corp.example/**",)))

        result = run_policy_checks(tmp_path, policy, fail_fast=False)

        denylist = next(c for c in result.checks if c.name == "dependency-denylist")
        assert not denylist.passed
        assert denylist.details[0].startswith("ghes.corp.example/example-org/lib:")


class TestAuditDirectSources:
    def _audit_allowlist(self, root: Path, manifest: str):
        (root / "apm.yml").write_text(textwrap.dedent(manifest), encoding="utf-8")
        policy = ApmPolicy(dependencies=DependencyPolicy(allow=ENTERPRISE_ALLOW))
        result = run_policy_checks(root, policy, fail_fast=False)
        return next(c for c in result.checks if c.name == "dependency-allowlist")

    def test_audit_gates_replacement_source_host(self, tmp_path: Path) -> None:
        allowlist = self._audit_allowlist(
            tmp_path,
            """\
            $schema: "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
            name: demo
            version: 1.0.0
            dependencies:
              apm:
                - example-org/app
            replace:
              - for:
                  git: https://github.com/example-org/app
                with:
                  git: https://ghes.unapproved.example/example-org/app
                  ref: patched
            """,
        )
        assert not allowlist.passed
        assert allowlist.details == [
            "ghes.unapproved.example/example-org/app: not in allowed sources"
        ]

    def test_audit_gates_direct_dev_dependency_host(self, tmp_path: Path) -> None:
        allowlist = self._audit_allowlist(
            tmp_path,
            """\
            name: demo
            version: 1.0.0
            dependencies:
              apm:
                - example-org/app
            devDependencies:
              apm:
                - ghes.unapproved.example/example-org/tool
            """,
        )
        assert not allowlist.passed
        assert allowlist.details == [
            "ghes.unapproved.example/example-org/tool: not in allowed sources"
        ]

    def test_audit_fails_closed_when_replacements_cannot_bind(self, tmp_path: Path) -> None:
        # A local replacement resolving to the consumer root cannot bind.
        (tmp_path / "apm.yml").write_text(
            textwrap.dedent(
                f"""\
                $schema: "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
                name: demo
                version: 1.0.0
                dependencies:
                  apm:
                    - example-org/app
                replace:
                  - for:
                      git: https://github.com/example-org/app
                    with:
                      path: ../{tmp_path.name}
                """
            ),
            encoding="utf-8",
        )
        policy = ApmPolicy(dependencies=DependencyPolicy(allow=ENTERPRISE_ALLOW))

        result = run_policy_checks(tmp_path, policy, fail_fast=False)

        assert [c.name for c in result.checks] == ["dependency-sources"]
        assert not result.checks[0].passed
        assert result.checks[0].message.startswith("Cannot resolve dependency sources")


def test_direct_and_denylist_details_name_the_effective_host() -> None:
    result = _check_dependency_denylist(
        [DependencyReference.parse("evil/repo")], DependencyPolicy(deny=("evil/**",))
    )
    assert result.details == ["github.com/evil/repo: denied by pattern: evil/**"]
