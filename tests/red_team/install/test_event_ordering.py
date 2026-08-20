"""Vector 5 -- event ordering + payload shape on the real install path.

Drives InstallService.run with a stubbed pipeline and on-disk TRUSTED
lifecycle scripts to prove pre-install fires before the pipeline and
post-install after it, and that the LifecycleEvent delivered to command
scripts (stdin) and HTTP scripts (payload) carries the right shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import LifecycleEvent, PackageInfo
from apm_cli.core.script_executors import _http_payload
from apm_cli.install.request import InstallRequest
from apm_cli.install.service import InstallService
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.results import InstallResult

from .conftest import PYEXE, fire_via_context, stub_policy, trust, write_project


def test_pre_runs_before_pipeline_before_post(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    order = tmp_path / "order.log"

    def append(token: str) -> str:
        return (
            f'{PYEXE} -c "import sys; '
            f'open(sys.argv[1], chr(97)).write(sys.argv[2]+chr(10))" '
            f'"{order}" "{token}"'
        )

    project.mkdir()
    lifecycle = {
        "pre-install": [{"type": "command", "run": append("pre")}],
        "post-install": [{"type": "command", "run": append("post")}],
    }
    from apm_cli.utils.yaml_io import dump_yaml

    apm_yml = project / "apm.yml"
    dump_yaml({"name": "rt", "version": "0", "lifecycle": lifecycle}, apm_yml)
    trust(apm_yml)

    def fake_pipeline(*_a, **_k) -> InstallResult:
        with open(order, "a") as f:
            f.write("pipeline\n")
        return InstallResult(installed_count=1)

    monkeypatch.setattr("apm_cli.install.pipeline.run_install_pipeline", fake_pipeline)

    pkg = APMPackage(name="rt", version="0", package_path=project)
    request = InstallRequest(apm_package=pkg)

    with stub_policy():
        result = InstallService().run(request)

    assert isinstance(result, InstallResult)
    assert order.read_text().split() == ["pre", "pipeline", "post"], (
        "lifecycle events fired out of order"
    )


def test_command_stdin_payload_shape(apm_home: Path, tmp_path: Path) -> None:
    """Command scripts receive event name + full working_directory on stdin."""
    project = tmp_path / "proj"
    payload_file = tmp_path / "payload.json"
    cmd = (
        f'{PYEXE} -c "import sys,pathlib; '
        f'pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())" '
        f'"{payload_file}"'
    )
    apm_yml = write_project(project, "post-install", [cmd])
    trust(apm_yml)

    fire_via_context(project, "post-install")

    payload = json.loads(payload_file.read_text(encoding="utf-8"))
    assert payload["event"] == "post-install"
    assert payload["working_directory"] == str(project), (
        "command scripts must get the full local working_directory"
    )


def test_http_payload_reduces_working_directory_to_basename() -> None:
    """HTTP payload must NOT leak the absolute path -- only the basename."""
    event = LifecycleEvent.create(
        event="post-install",
        packages=[PackageInfo(name="rt/pkg", reference="v1")],
        scope="project",
        working_directory="/home/alice/secret-project",
    )
    payload = json.loads(_http_payload(event))
    assert payload["working_directory"] == "secret-project", (
        "HTTP payload leaked the absolute working_directory path"
    )
    assert "alice" not in payload["working_directory"]
