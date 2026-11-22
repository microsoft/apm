"""``apm pack`` judges the packages it would ship against ``dependencies.deny`` only."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.bundle.pack_policy import pack_policy_preflight
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-policy-violation-subjects")]


def _project(root: Path, declared: list[str], locked: list[str]) -> Path:
    root.mkdir()
    apm = "\n".join(f"    - {ref}" for ref in declared)
    (root / "apm.yml").write_text(
        f"name: proj\nversion: 1.0.0\ndependencies:\n  apm:\n{apm or '    []'}\n",
        encoding="utf-8",
    )
    lock = LockFile()
    for repo in locked:
        lock.add_dependency(LockedDependency(repo_url=repo, resolved_commit="a" * 40))
    (root / "apm.lock.yaml").write_text(lock.to_yaml(), encoding="utf-8")
    return root


def _policy(**dependencies: object) -> PolicyFetchResult:
    policy = ApmPolicy(enforcement="block", dependencies=DependencyPolicy(**dependencies))
    return PolicyFetchResult(policy=policy, source="org:acme/.github", outcome="found")


def _pack(project: Path, fetch: PolicyFetchResult) -> None:
    with patch("apm_cli.policy.install_preflight.discover_policy_with_chain", return_value=fetch):
        pack_policy_preflight(project, None)


def test_org_require_and_allow_lists_do_not_refuse_a_pack(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path / "p", ["acme/tool"], ["acme/tool"])

    _pack(project, _policy(require=("acme/baseline",), allow=("other/**",)))

    captured = capsys.readouterr()
    assert "Policy violation" not in captured.out + captured.err


@pytest.mark.parametrize("locked", [["acme/tool"], []])
def test_denied_declared_package_refuses_whatever_the_lock_holds(
    tmp_path: Path, locked: list[str]
) -> None:
    project = _project(tmp_path / "p", ["acme/tool"], locked)

    with pytest.raises(ValueError, match=r"Pack blocked by org policy: github\.com/acme/tool"):
        _pack(project, _policy(deny=("acme/tool",)))
