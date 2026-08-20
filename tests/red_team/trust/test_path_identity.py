"""Vector 5 -- path identity: trust is keyed by Path.resolve().

Trust must follow the real resolved file: equivalent path spellings share
trust, but a symlink repointed to a different file after trust must NOT
inherit it.  All assertions describe secure behavior and pass on head.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.core.script_trust import is_project_scripts_trusted, trust_project_scripts

BENIGN = "lifecycle:\n  post-install:\n    - type: command\n      bash: echo hi\n"
EVIL = "lifecycle:\n  post-install:\n    - type: command\n      bash: echo PWNED\n"


def test_relative_and_absolute_spellings_share_trust(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """./apm.yml, apm.yml and the absolute path resolve to one trust key."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text(BENIGN, encoding="utf-8")

    trust_project_scripts(project / "apm.yml")

    monkeypatch.chdir(project)
    assert is_project_scripts_trusted(Path("apm.yml"))
    assert is_project_scripts_trusted(Path("./apm.yml"))
    assert is_project_scripts_trusted((project / "apm.yml").resolve())


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_trust_through_symlink_follows_real_file(apm_home: Path, tmp_path: Path) -> None:
    """Trusting via a symlink keys trust on the real (resolved) target."""
    real = tmp_path / "real-apm.yml"
    real.write_text(BENIGN, encoding="utf-8")
    link = tmp_path / "apm.yml"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted")

    trust_project_scripts(link)
    assert is_project_scripts_trusted(link)
    assert is_project_scripts_trusted(real), "real file shares the resolved trust key"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_repointed_symlink_does_not_inherit_trust(apm_home: Path, tmp_path: Path) -> None:
    """Repointing a trusted symlink to a different file must drop trust."""
    benign = tmp_path / "benign.yml"
    benign.write_text(BENIGN, encoding="utf-8")
    evil = tmp_path / "evil.yml"
    evil.write_text(EVIL, encoding="utf-8")

    link = tmp_path / "apm.yml"
    try:
        link.symlink_to(benign)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted")

    trust_project_scripts(link)
    assert is_project_scripts_trusted(link)

    link.unlink()
    link.symlink_to(evil)
    assert not is_project_scripts_trusted(link), (
        "repointed symlink inherited trust for attacker-controlled target"
    )
