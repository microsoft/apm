"""E2 red-team cells for the no-op fast paths (v0.39.0 RC, train #5406).

Each cell is red on the RC when the finding holds. They reuse the verified-state
record harness of test_verdict_record.py.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from apm_cli.install import verdict_record
from tests.integration.test_verdict_record import (  # noqa: F401
    _attest,
    _attested_hit,
    _record,
    project,
)

pytestmark = [pytest.mark.component, pytest.mark.promise("P-INV-I2")]


def _link_target_root(root: Path, name: str = ".github") -> Path:
    """Replace a governed target root with a symlink to an identical copy outside."""
    outside = root.parent / "outside" / name
    outside.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root / name, outside, symlinks=True)
    shutil.rmtree(root / name)
    (root / name).symlink_to(outside, target_is_directory=True)
    return outside


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_a_target_root_swapped_for_a_link_misses_the_replay(project: Path, capsys) -> None:  # noqa: F811
    """The engine refuses a linked deploy root (diff_apply._within); a hit must not."""
    _record()
    _link_target_root(project)
    capsys.readouterr()

    hit = verdict_record.replay(["install"])

    assert (hit, capsys.readouterr().out) == (False, "")


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_a_target_root_swapped_for_a_link_misses_the_attested_hit(project: Path) -> None:  # noqa: F811
    """An attested hit skips _plan_files, the engine's linked-path refusal."""
    _attest(project)
    assert _attested_hit() is True, "positive control: run 2 matches the attestation"

    _link_target_root(project)

    assert _attested_hit() is False


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_a_target_root_swapped_for_a_link_is_refused_by_the_engine_fast_path(
    project: Path,  # noqa: F811
) -> None:
    """Control: the engine's own fast path declines a linked deploy root."""
    from apm_cli.install.diff_apply import Decline, _within

    _link_target_root(project)

    with pytest.raises(Decline, match="symlink in a deployed path"):
        _within(project, ".github/prompts/a.prompt.md")
