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
