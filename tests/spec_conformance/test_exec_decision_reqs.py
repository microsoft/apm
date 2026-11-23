"""req-sc-009 / req-sc-014 through the one executable trust owner (#4018).

The install gate fails closed: only an explicit authorization (the gate
disabled because no layer opted in) approves every executable; a missing or
omitted approvals map approves nothing. Bin deployment composes the gate's
verdict with per-invocation consent, and the gate wins.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import apm_cli.security.executables as ex
from apm_cli.install.exec_gate import check_executable_approval, resolve_bin_skip
from apm_cli.security.executables import build_exec_trust_context, materialize_exec_map

PKG = "acme/tools"


@pytest.fixture(autouse=True)
def _isolated_user_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "config.json")
    monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "approvals.yml")


def _package(root: Path) -> SimpleNamespace:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    return SimpleNamespace(
        dependency_ref=None, package=SimpleNamespace(name=PKG, version=""), install_path=root
    )


def _allow_map(project_data):
    return materialize_exec_map(build_exec_trust_context(policy=None, project_data=project_data))


@pytest.mark.req("req-sc-009")
@pytest.mark.parametrize(
    ("allow_map", "expected"),
    [
        (lambda: _allow_map({"executables": {"allow": {}}}), (False,) * 5),
        (lambda: _allow_map({"executables": {"allow": {PKG: {"bin": True}}}}), (False, True)),
        (lambda: None, (False,) * 5),
        (lambda: _allow_map(None), (True,) * 5),
    ],
    ids=["unlisted-denied", "listed-type-approved", "missing-map-denied", "gate-disabled"],
)
def test_executable_gate_fails_closed_without_explicit_authorization(
    tmp_path: Path, allow_map, expected
) -> None:
    approvals = check_executable_approval(PKG, _package(tmp_path / "pkg"), allow_map())
    assert approvals[: len(expected)] == expected


@pytest.mark.req("req-sc-014")
@pytest.mark.parametrize(
    ("bin_approved", "trust_bin", "non_interactive", "expected"),
    [
        (True, None, True, (True, "not_trusted")),
        (True, True, True, (False, None)),
        (True, False, False, (True, "not_trusted")),
        (False, True, False, (True, "not_approved")),
    ],
    ids=["non-tty-default-deny", "opt-in", "opt-out", "gate-wins"],
)
def test_bin_consent_composes_after_the_gate(
    bin_approved, trust_bin, non_interactive, expected
) -> None:
    assert resolve_bin_skip(bin_approved, trust_bin, non_interactive=non_interactive) == expected


@pytest.mark.req("req-sc-009")
@pytest.mark.parametrize(
    ("allow", "expected"),
    [({}, {"acme/tools": ("not approved", ("lsp",))}), ({PKG: {"lsp": True}}, {})],
    ids=["unlisted-retired-with-its-kind", "listed-kept"],
)
def test_gate_retirement_withholds_each_unapproved_executable_kind(
    tmp_path: Path, allow, expected
) -> None:
    """#4567: a survivor deployed before the gate opted in loses each unapproved kind."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.install.exec_gate import locked_hooks_retired
    from apm_cli.utils.yaml_io import dump_yaml

    modules = tmp_path / "apm_modules"
    (modules / PKG).mkdir(parents=True)
    server = {
        "name": "s",
        "command": "inert",
        "args": ["--stdio"],
        "extensionToLanguage": {".x": "x"},
    }
    dump_yaml(
        {"name": "tools", "version": "1.0.0", "dependencies": {"lsp": [server]}},
        modules / PKG / "apm.yml",
    )
    lockfile = LockFile()
    lockfile.add_dependency(LockedDependency(repo_url=PKG, resolved_commit="a" * 40))
    trust = build_exec_trust_context(policy=None, project_data={"executables": {"allow": allow}})

    assert locked_hooks_retired(lockfile, modules, lambda: trust, pinned=set()) == expected


@pytest.mark.req("req-sc-009")
def test_gate_retirement_names_only_the_kinds_the_lock_recorded_as_deployed(
    tmp_path: Path,
) -> None:
    """#4567: a kind the lock never recorded as deployed is not reported as removed."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.install.exec_gate import locked_hooks_retired
    from apm_cli.utils.yaml_io import dump_yaml

    package = tmp_path / "apm_modules" / PKG
    hooks = package / ".apm" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "session.json").write_text(
        '{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo"}]}]}}',
        encoding="utf-8",
    )
    server = {"name": "s", "command": "inert", "args": [], "extensionToLanguage": {".x": "x"}}
    dump_yaml(
        {"name": "tools", "version": "1.0.0", "dependencies": {"lsp": [server]}},
        package / "apm.yml",
    )
    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(
            repo_url=PKG,
            resolved_commit="a" * 40,
            exec_status=ex.TRUST_GATED,
            exec_allowed_types=["hooks"],
        )
    )
    trust = build_exec_trust_context(policy=None, project_data={"executables": {"allow": {}}})

    retired = locked_hooks_retired(lockfile, tmp_path / "apm_modules", lambda: trust, pinned=set())

    assert retired == {"acme/tools": ("not approved", ("hooks",))}
