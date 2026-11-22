"""``apm compile`` applies the org dependency policy before it renders installed content."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from apm_cli.commands.compile.cli import (
    _dependency_policy_allows,
    _enforce_dependency_policy,
    _rendered_dependencies,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-policy-violation-subjects")]

_RESOLVE = "apm_cli.policy.outcome_routing.resolve_trust_policy"


class _Logger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def error(self, message, symbol="error"):
        self.lines.append(("error", message))

    def warning(self, message, symbol="warning"):
        self.lines.append(("warning", message))

    def refusal(self, message):
        self.lines.append(("refusal", message))

    def progress(self, message, symbol=None):
        self.lines.append(("progress", message))

    def verbose_detail(self, message):
        self.lines.append(("verbose", message))


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
        patch(
            "apm_cli.install.exec_gate.policy_denied_dependencies",
            return_value={"acme/denied": "org policy: denied by pattern"},
        ),
    ):
        try:
            _enforce_dependency_policy(logger, [project])
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


def test_a_project_without_installed_packages_never_resolves_policy(tmp_path: Path) -> None:
    logger = _Logger()
    with patch(_RESOLVE) as resolve:
        _enforce_dependency_policy(logger, [_project(tmp_path, locked=False)])

    resolve.assert_not_called()
    assert logger.lines == []
    assert not (tmp_path / "AGENTS.md").exists()


def test_installed_packages_are_judged_without_a_lock_row(tmp_path: Path) -> None:
    """A lost lock still renders apm.yml's installed dependencies; they are judged too."""
    (tmp_path / "apm.yml").write_text(
        yaml.safe_dump(
            {"name": "p", "version": "1.0.0", "dependencies": {"apm": ["acme/declared"]}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "apm_modules" / "acme" / "declared").mkdir(parents=True)
    (tmp_path / "apm_modules" / "acme" / "absent").mkdir(parents=True)
    lock = LockFile()
    lock.local_staged_files = ["apm_modules/bundle-x/.apm/instructions/a.instructions.md"]
    lock.write(tmp_path / "apm.lock.yaml")

    keys = [key for key, _ref in _rendered_dependencies(tmp_path, user_scope=False)]

    assert keys == ["_local/bundle-x", "acme/declared"]
    assert (tmp_path / "apm.yml").is_file()


def test_a_redirected_install_root_is_judged_too(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    _project(redirected)
    logger = _Logger()
    with (
        patch(_RESOLVE, return_value=SimpleNamespace(enforcement="block")),
        patch(
            "apm_cli.install.exec_gate.policy_denied_dependencies",
            side_effect=lambda subjects, _policy: {
                key: "org policy: denied" for key, _ in subjects
            },
        ),
    ):
        allowed = _dependency_policy_allows(logger, [source, redirected])

    assert allowed is False
    assert ("error", "acme/denied: org policy: denied") in logger.lines
    assert not (source / "AGENTS.md").exists()


def test_global_compile_stops_when_policy_refuses(tmp_path: Path, monkeypatch) -> None:
    from apm_cli.commands.compile import cli as compile_cli

    apm_home = tmp_path / "apm-home"
    (apm_home / "apm_modules").mkdir(parents=True)
    _project(apm_home)
    monkeypatch.setattr("apm_cli.core.scope.get_apm_dir", lambda scope=None: apm_home)
    logger = _Logger()
    with (
        patch(_RESOLVE, return_value=SimpleNamespace(enforcement="block")),
        patch(
            "apm_cli.install.exec_gate.policy_denied_dependencies",
            return_value={"acme/denied": "org policy: denied by pattern"},
        ),
        patch("apm_cli.compilation.compile_user_root_contexts") as render,
    ):
        code = compile_cli._handle_global_flag(dry_run=False, logger=logger)

    assert code == 1
    render.assert_not_called()
    assert ("error", "acme/denied: org policy: denied by pattern") in logger.lines
    assert not (apm_home / "AGENTS.md").exists()
