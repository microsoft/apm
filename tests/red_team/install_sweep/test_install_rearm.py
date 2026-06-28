"""Vector 7 -- install wiring + trust re-arm gate on the real firing path.

Confirms, through build_runner_from_context (the exact call the install
service uses) and through InstallService._build_script_runner itself:

- untrusted project does NOT fire on install (scripts skipped),
- trusted project DOES fire,
- APM_NO_SCRIPTS suppresses even when trusted,
- org executables.deny_all suppresses even when trusted,
- trust-then-edit re-arms the gate: editing lifecycle: after trusting
  drops the (now-untrusted) edited scripts, so an attacker who edits the
  lifecycle block AFTER trust cannot smuggle a new command through.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.core.lifecycle_scripts import (
    LifecycleEvent,
    PackageInfo,
    build_runner_from_context,
)
from apm_cli.core.script_trust import trust_project_scripts
from apm_cli.utils.yaml_io import dump_yaml

from .conftest import PYEXE


def _touch_cmd(sentinel: Path) -> str:
    return (
        f'{PYEXE} -c "import sys,pathlib; '
        f'pathlib.Path(sys.argv[1]).write_text(chr(120))" "{sentinel}"'
    )


def _write_lifecycle(project: Path, commands: list[str]) -> Path:
    project.mkdir(parents=True, exist_ok=True)
    lifecycle = {"post-install": [{"type": "command", "run": c} for c in commands]}
    apm_yml = project / "apm.yml"
    dump_yaml({"name": "rt-pkg", "version": "0.0.0", "lifecycle": lifecycle}, apm_yml)
    return apm_yml


@contextmanager
def _stub_policy(deny_all: bool = False):
    if not deny_all:
        with patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=None):
            yield
        return

    class _Exec:
        deny_all = True

    class _Pol:
        executables = _Exec()

    class _Result:
        policy = _Pol()

    with patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=_Result()):
        yield


def _fire(project: Path, *, deny_all: bool = False) -> None:
    with _stub_policy(deny_all=deny_all):
        runner = build_runner_from_context(project_root=str(project))
    evt = LifecycleEvent.create(
        event="post-install",
        packages=[PackageInfo(name="rt/pkg", reference="v1")],
        scope="project",
        working_directory=str(project),
    )
    for t in runner.fire("post-install", evt):
        t.join(timeout=10)


def test_untrusted_install_does_not_fire(apm_home: Path, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    sentinel = tmp_path / "ran"
    _write_lifecycle(project, [_touch_cmd(sentinel)])
    _fire(project)
    assert not sentinel.exists(), "untrusted project executed scripts on install"


def test_trusted_install_fires(apm_home: Path, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    sentinel = tmp_path / "ran"
    apm_yml = _write_lifecycle(project, [_touch_cmd(sentinel)])
    trust_project_scripts(apm_yml)
    _fire(project)
    assert sentinel.exists(), "trusted project failed to run scripts on install"


def test_apm_no_scripts_suppresses_trusted(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    sentinel = tmp_path / "ran"
    apm_yml = _write_lifecycle(project, [_touch_cmd(sentinel)])
    trust_project_scripts(apm_yml)
    monkeypatch.setenv("APM_NO_SCRIPTS", "1")
    _fire(project)
    assert not sentinel.exists(), "APM_NO_SCRIPTS did not suppress trusted scripts"


def test_deny_all_suppresses_trusted(apm_home: Path, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    sentinel = tmp_path / "ran"
    apm_yml = _write_lifecycle(project, [_touch_cmd(sentinel)])
    trust_project_scripts(apm_yml)
    _fire(project, deny_all=True)
    assert not sentinel.exists(), "deny_all did not suppress trusted scripts"


def test_trust_then_edit_lifecycle_rearms_gate(apm_home: Path, tmp_path: Path) -> None:
    """Trust the current lifecycle, then SWAP IN a new malicious command.

    The fingerprint is keyed to the trusted subtree, so the edited block is
    untrusted again: the attacker's injected command must NOT run.
    """
    project = tmp_path / "proj"
    benign = tmp_path / "benign"
    injected = tmp_path / "injected"
    apm_yml = _write_lifecycle(project, [_touch_cmd(benign)])
    trust_project_scripts(apm_yml)

    # Attacker edits lifecycle AFTER trust, adding a new command.
    _write_lifecycle(project, [_touch_cmd(benign), _touch_cmd(injected)])

    _fire(project)
    assert not injected.exists(), (
        "RE-ARM FAILED: an edited lifecycle: block ran after trust -- editing "
        "must revoke trust for the whole subtree."
    )
    assert not benign.exists(), (
        "the edited (now-untrusted) block partially ran -- trust must be "
        "all-or-nothing on the lifecycle subtree."
    )


def test_install_service_runner_honours_gate(apm_home: Path, tmp_path: Path) -> None:
    """InstallService._build_script_runner must drop untrusted project scripts."""
    from apm_cli.install.service import InstallService

    project = tmp_path / "proj"
    sentinel = tmp_path / "ran"
    _write_lifecycle(project, [_touch_cmd(sentinel)])

    class _Pkg:
        package_path = str(project)

    class _Req:
        apm_package = _Pkg()
        logger = None
        verbose = False

    with _stub_policy():
        runner = InstallService._build_script_runner(_Req())
    # Untrusted -> no project scripts kept.
    assert runner.scripts_for_event("post-install") == [], (
        "InstallService kept untrusted project scripts -- gate not applied "
        "at the install wiring boundary."
    )
