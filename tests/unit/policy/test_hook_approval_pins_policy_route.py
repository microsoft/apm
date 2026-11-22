"""hook-approval-pins judges withheld hooks on the audit's one policy discovery (#4821)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.errors import PolicyViolationError
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage
from apm_cli.policy.ci_checks import _check_hook_approval_pins
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.schema import ApmPolicy

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-executable-item-admission")]

_HOOK = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo h"}]}]}}
_RESTORED = ".github/hooks/h-hook-session.json"


def _project(tmp_path: Path) -> tuple[Path, LockFile, APMPackage]:
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: consumer\nversion: 1.0.0\npolicy:\n  fetch_failure_default: block\n",
        encoding="utf-8",
    )
    dep = LockedDependency(repo_url="acme/h-hook", resolved_commit="a" * 40, exec_status="denied")
    lock = LockFile()
    lock.add_dependency(dep)
    root = dep.to_dependency_ref().get_install_path(project / "apm_modules")
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "session.json").write_text(json.dumps(_HOOK), encoding="utf-8")
    (root / "apm.yml").write_text("name: h-hook\nversion: 1.0.0\n", encoding="utf-8")
    (project / ".github" / "hooks").mkdir(parents=True)
    (project / _RESTORED).write_text("{}", encoding="utf-8")
    return project, lock, APMPackage.from_apm_yml(project / "apm.yml")


def _pins(tmp_path: Path, fetch: PolicyFetchResult | None):
    project, lock, manifest = _project(tmp_path)

    def decide(provider, *_args):
        provider()  # the gate consults the trust context, as current_exec_decision does
        return "denied", ()

    with (
        patch("apm_cli.install.exec_gate.current_exec_decision", side_effect=decide),
        # The audit's auto-discovery would block (an unreachable org policy).
        patch(
            "apm_cli.security.executables.resolve_project_exec_allow_map",
            side_effect=PolicyViolationError("Blocked by org policy: auto-discovered"),
        ),
    ):
        result = _check_hook_approval_pins(
            project,
            project,
            lock,
            manifest,
            user_scope=False,
            no_policy=False,
            prepared_replay=None,
            targets=[KNOWN_TARGETS["copilot"]],
            policy_fetch=None if fetch is None else (lambda: fetch),
        )
    return result, project


def test_a_valid_explicit_policy_still_checks_withheld_hook_files(tmp_path: Path) -> None:
    """--policy names a valid file: the route does not block, so the scan must run."""
    fetch = PolicyFetchResult(policy=ApmPolicy(), source="file:valid.yml", outcome="found")

    result, project = _pins(tmp_path, fetch)

    assert not result.passed
    assert any(detail.startswith(f"{_RESTORED}: hook path of") for detail in result.details)
    assert (project / _RESTORED).read_text(encoding="utf-8") == "{}"


def test_a_blocking_route_is_left_to_the_policy_outcome(tmp_path: Path) -> None:
    """The route blocks on the same discovery and reports it; this check does not repeat it."""
    fetch = PolicyFetchResult(policy=None, source="org:acme/.github", outcome="empty")

    result, project = _pins(tmp_path, fetch)

    assert result.passed, result.message
    assert (project / _RESTORED).read_text(encoding="utf-8") == "{}"


def test_without_an_audit_discovery_a_policy_block_fails_closed(tmp_path: Path) -> None:
    result, project = _pins(tmp_path, None)

    assert not result.passed
    assert "Blocked by org policy" in result.message
    assert (project / _RESTORED).read_text(encoding="utf-8") == "{}"
