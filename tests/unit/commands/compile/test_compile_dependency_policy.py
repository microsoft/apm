"""``apm compile`` applies the org dependency policy before it renders installed content."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apm_cli.commands.compile.cli import _enforce_dependency_policy
from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-policy-violation-subjects")]

_RESOLVE = "apm_cli.policy.outcome_routing.resolve_trust_policy"
_DENIED = "apm_cli.install.exec_gate.policy_denied_locked_packages"


class _Logger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def error(self, message, symbol="error"):
        self.lines.append(("error", message))

    def warning(self, message, symbol="warning"):
        self.lines.append(("warning", message))

    def refusal(self, message):
        self.lines.append(("refusal", message))


def _project(tmp_path: Path, *, locked: bool = True) -> Path:
    lock = LockFile()
    if locked:
        lock.dependencies["acme/denied"] = LockedDependency(repo_url="acme/denied")
    lock.write(tmp_path / "apm.lock.yaml")
    return tmp_path


def _run(project: Path, enforcement: str) -> tuple[int | None, _Logger]:
    logger = _Logger()
    policy = SimpleNamespace(enforcement=enforcement)
    with (
        patch(_RESOLVE, return_value=policy),
        patch(_DENIED, return_value={"acme/denied": "org policy: denied by pattern"}),
    ):
        try:
            _enforce_dependency_policy(logger, project)
        except SystemExit as exc:
            return exc.code, logger
    return None, logger


def test_block_enforcement_refuses_and_names_the_dependency(tmp_path: Path) -> None:
    code, logger = _run(_project(tmp_path), "block")

    assert code == 1
    assert ("error", "acme/denied: org policy: denied by pattern") in logger.lines
    assert any(kind == "refusal" and "apm.yml" in text for kind, text in logger.lines)
    assert not (tmp_path / "AGENTS.md").exists()


def test_warn_enforcement_reports_and_continues(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = (project / "apm.lock.yaml").read_bytes()
    code, logger = _run(project, "warn")

    assert code is None
    assert logger.lines == [("warning", "acme/denied: org policy: denied by pattern")]
    assert (project / "apm.lock.yaml").read_bytes() == before, "the check is read-only"


def test_off_enforcement_is_silent(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = (project / "apm.lock.yaml").read_bytes()
    code, logger = _run(project, "off")

    assert (code, logger.lines) == (None, [])
    assert (project / "apm.lock.yaml").read_bytes() == before, "the check is read-only"


def test_a_project_without_locked_dependencies_never_resolves_policy(tmp_path: Path) -> None:
    logger = _Logger()
    with patch(_RESOLVE) as resolve:
        _enforce_dependency_policy(logger, _project(tmp_path, locked=False))

    resolve.assert_not_called()
    assert logger.lines == []
    assert not (tmp_path / "AGENTS.md").exists()
