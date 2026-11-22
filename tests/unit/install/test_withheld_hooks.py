"""Unrecorded files at a withheld package's own hook path are found, read-only."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.exec_gate import ExecPolicyUnavailable
from apm_cli.install.withheld_hooks import locked_withheld_hook_files, withheld_hook_files
from apm_cli.integration.targets import KNOWN_TARGETS

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-executable-item-admission")]

_HOOK = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo h"}]}]}}


def _package(root: Path, *names: str) -> SimpleNamespace:
    hooks = root / "h-hook" / "hooks"
    hooks.mkdir(parents=True)
    for name in names:
        (hooks / name).write_text(json.dumps(_HOOK), encoding="utf-8")
    return SimpleNamespace(install_path=hooks.parent, package=SimpleNamespace(name="h-hook"))


@pytest.mark.parametrize(
    "sources",
    [("session.json",), ("hooks-copilot.json", "hooks-claude.json")],
    ids=["one-source", "legacy-per-target-names"],
)
def test_an_unrecorded_file_at_the_copilot_hook_path_is_found(
    tmp_path: Path, sources: tuple[str, ...]
) -> None:
    package = _package(tmp_path / "modules", *sources)
    project = tmp_path / "project"
    hooks = project / ".github" / "hooks"
    hooks.mkdir(parents=True)
    copilot = KNOWN_TARGETS["copilot"]
    stem = Path(sources[0]).stem
    (hooks / f"h-hook-{stem}.json").write_text("{}", encoding="utf-8")
    (hooks / "someone-else.json").write_text("{}", encoding="utf-8")

    found = withheld_hook_files(package, project, [copilot], recorded=set())

    assert found == [f".github/hooks/h-hook-{stem}.json"]
    assert withheld_hook_files(package, project, [copilot], recorded=set(found)) == []
    assert withheld_hook_files(package, project, [KNOWN_TARGETS["claude"]], recorded=set()) == []
    assert (hooks / f"h-hook-{stem}.json").read_text(encoding="utf-8") == "{}"


def _locked(tmp_path: Path, target_subset: list[str]) -> tuple[LockFile, Path, Path]:
    dep = LockedDependency(
        repo_url="acme/h-hook",
        resolved_commit="a" * 40,
        exec_status="denied",
        target_subset=target_subset,
    )
    lock = LockFile()
    lock.add_dependency(dep)
    modules = tmp_path / "apm_modules"
    root = dep.to_dependency_ref().get_install_path(modules)
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "session.json").write_text(json.dumps(_HOOK), encoding="utf-8")
    (root / "apm.yml").write_text("name: h-hook\nversion: 1.0.0\n", encoding="utf-8")
    project = tmp_path / "project"
    (project / ".github" / "hooks").mkdir(parents=True)
    (project / ".github" / "hooks" / "h-hook-session.json").write_text("{}", encoding="utf-8")
    return lock, modules, project


@pytest.mark.parametrize(
    ("target_subset", "expected"),
    [
        ([], [(".github/hooks/h-hook-session.json", "acme/h-hook", "hooks denied")]),
        (["claude"], []),
    ],
    ids=["unrestricted", "restricted-to-claude"],
)
def test_a_dependency_restricted_away_from_copilot_is_not_reported(
    tmp_path: Path, target_subset: list[str], expected: list[tuple[str, str, str]]
) -> None:
    lock, modules, project = _locked(tmp_path, target_subset)
    targets = [KNOWN_TARGETS["copilot"], KNOWN_TARGETS["claude"]]

    with patch("apm_cli.install.exec_gate.current_exec_decision", return_value=("denied", ())):
        found = locked_withheld_hook_files(lock, modules, lambda: None, project, targets)

    assert found == expected
    assert (project / ".github" / "hooks" / "h-hook-session.json").is_file()


def test_hook_paths_that_cannot_be_computed_fail_closed(tmp_path: Path) -> None:
    lock, modules, project = _locked(tmp_path, [])

    with (
        patch("apm_cli.install.exec_gate.current_exec_decision", return_value=("denied", ())),
        patch(
            "apm_cli.integration.copilot_hook_paths.copilot_hook_destinations",
            side_effect=ValueError("unreadable hook source"),
        ),
    ):
        found = locked_withheld_hook_files(
            lock, modules, lambda: None, project, [KNOWN_TARGETS["copilot"]]
        )

    assert found == [
        (
            "",
            "acme/h-hook",
            "hooks denied; its hook paths cannot be checked: unreadable hook source",
        )
    ]
    assert (project / ".github" / "hooks" / "h-hook-session.json").is_file()


@pytest.mark.parametrize(
    ("target_subset", "error", "reported"),
    [
        ([], PermissionError("bin/ unreadable"), True),
        ([], ExecPolicyUnavailable("bin/ unreadable"), True),
        (["claude"], PermissionError("bin/ unreadable"), False),
    ],
    ids=["os-error", "policy-unavailable", "restricted-to-claude"],
)
def test_an_unreadable_decision_fails_closed_where_copilot_is_eligible(
    tmp_path: Path, target_subset: list[str], error: Exception, reported: bool
) -> None:
    lock, modules, project = _locked(tmp_path, target_subset)

    with patch("apm_cli.install.exec_gate.current_exec_decision", side_effect=error):
        found = locked_withheld_hook_files(
            lock, modules, lambda: None, project, [KNOWN_TARGETS["copilot"]]
        )

    expected = [("", "acme/h-hook", "its executables decision cannot be read: bin/ unreadable")]
    assert found == (expected if reported else [])
    assert (project / ".github" / "hooks" / "h-hook-session.json").is_file()


@pytest.mark.parametrize("referent_recorded", [False, True], ids=["unrecorded", "recorded"])
def test_a_symlinked_hook_path_is_named_as_the_link(
    tmp_path: Path, referent_recorded: bool
) -> None:
    """The link is compared and reported as itself, never as its referent."""
    package = _package(tmp_path / "modules", "session.json")
    project = tmp_path / "project"
    hooks = project / ".github" / "hooks"
    hooks.mkdir(parents=True)
    (project / "user-hook.json").write_text("{}", encoding="utf-8")
    (hooks / "h-hook-session.json").symlink_to(project / "user-hook.json")
    recorded = {"user-hook.json"} if referent_recorded else set()

    found = withheld_hook_files(package, project, [KNOWN_TARGETS["copilot"]], recorded=recorded)

    assert found == [".github/hooks/h-hook-session.json"]
    assert (hooks / "h-hook-session.json").is_symlink()
