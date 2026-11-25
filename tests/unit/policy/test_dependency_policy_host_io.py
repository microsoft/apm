"""Host-aware dependency allow/deny matching (#3509) (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency import DependencyReference
from apm_cli.policy.policy_checks import (
    run_dependency_policy_checks,
    run_policy_checks,
)
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-read-only-lockfile-path", "P-OWN-policy-mcp-transport-projection"),
]

ENTERPRISE_ALLOW = (
    "ghes.corp.example/**",
    "contoso.ghe.com/**",
    "github.com/example-org/**",
)


@pytest.fixture(autouse=True)
def _default_github_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_HOST", raising=False)


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
