"""Vector 1 -- TOCTOU read-swap against the trust gate.

build_runner_from_context reads apm.yml TWICE through
apm_cli.utils.yaml_io.load_yaml:

  1. discover_scripts()  -> parse_apm_yml_lifecycle() -> load_yaml()   (what RUNS)
  2. is_project_scripts_trusted() -> script_file_fingerprint() -> load_yaml()  (what is CHECKED)

Because the two reads are independent, content that is malicious on the
first read but matches the trusted hash on the second read executes while
the gate reports "trusted".  The SECURE contract is: the bytes that are
EXECUTED must be the bytes that were TRUSTED.

Each test below asserts that secure contract, so a genuine break shows up
as a FAILING test on head c03bced50.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apm_cli.core.lifecycle_scripts import (
    LifecycleEvent,
    PackageInfo,
    build_runner_from_context,
)
from apm_cli.core.script_trust import trust_project_scripts
from apm_cli.utils import yaml_io

from .conftest import sentinel_command

BENIGN_YML = 'name: pkg\nlifecycle:\n  post-install:\n    - type: command\n      run: "true"\n'


def _malicious_yml(sentinel: Path) -> str:
    """Valid YAML whose post-install command creates sentinel when run."""
    data = {
        "name": "pkg",
        "lifecycle": {"post-install": [{"type": "command", "run": sentinel_command(sentinel)}]},
    }
    return yaml.safe_dump(data, default_flow_style=False)


def _fire(project: Path) -> None:
    runner = build_runner_from_context(project_root=str(project))
    event = LifecycleEvent.create(
        event="post-install",
        packages=[PackageInfo(name="t/pkg", reference="v1")],
        scope="project",
        working_directory=str(project),
    )
    for thread in runner.fire("post-install", event):
        thread.join(timeout=10)


@pytest.mark.usefixtures("hermetic_sources")
def test_toctou_disk_swap_between_reads(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real on-disk swap between the discovery and fingerprint reads.

    The file is malicious when discovery reads it, then swapped to the
    trusted benign content before the fingerprint read.  On secure code
    the executed bytes equal the trusted bytes, so the malicious sentinel
    must NOT appear.
    """
    project = tmp_path / "project"
    project.mkdir()
    apm_yml = project / "apm.yml"
    sentinel = project / "PWNED.txt"

    apm_yml.write_text(BENIGN_YML, encoding="utf-8")
    trust_project_scripts(apm_yml)

    apm_yml.write_text(_malicious_yml(sentinel), encoding="utf-8")

    real_load = yaml_io.load_yaml
    state = {"swapped": False}

    def swapping_load(path: str | Path):
        p = Path(path)
        data = real_load(path)
        if not state["swapped"] and p == apm_yml:
            state["swapped"] = True
            apm_yml.write_text(BENIGN_YML, encoding="utf-8")
        return data

    monkeypatch.setattr(yaml_io, "load_yaml", swapping_load)

    _fire(project)

    assert not sentinel.exists(), (
        "TOCTOU: malicious lifecycle executed while the gate fingerprinted "
        "the swapped-in benign content -- executed bytes != trusted bytes"
    )


@pytest.mark.usefixtures("hermetic_sources")
def test_toctou_independent_reads_disagree(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discovery read returns malicious; the fingerprint read returns benign.

    Models any source of read divergence (concurrent writer, networked FS,
    re-parse nondeterminism).  Secure code single-sources the bytes, so the
    malicious sentinel must NOT appear.
    """
    project = tmp_path / "project"
    project.mkdir()
    apm_yml = project / "apm.yml"
    sentinel = project / "PWNED.txt"

    apm_yml.write_text(BENIGN_YML, encoding="utf-8")
    trust_project_scripts(apm_yml)

    real_load = yaml_io.load_yaml
    malicious_data = {
        "name": "pkg",
        "lifecycle": {"post-install": [{"type": "command", "run": sentinel_command(sentinel)}]},
    }
    state = {"calls": 0}

    def diverging_load(path: str | Path):
        p = Path(path)
        if p == apm_yml:
            state["calls"] += 1
            if state["calls"] == 1:
                return malicious_data
        return real_load(path)

    monkeypatch.setattr(yaml_io, "load_yaml", diverging_load)

    _fire(project)

    # Single-sourcing the project tier (one parse feeds both the executed
    # entries and the trust fingerprint) collapses the read count to 1 --
    # the divergence window the original >=2 assertion probed no longer
    # exists. The security PROPERTY (no malicious script runs) is what
    # matters and is asserted below.
    assert state["calls"] >= 1, "expected at least one read of apm.yml"
    assert not sentinel.exists(), (
        "TOCTOU: gate trusted the second (benign) read while the first "
        "(malicious) read is what was scheduled to execute"
    )
