"""Vector 6 -- tier confusion.

Policy and user tiers are never gated (developer-controlled), but a PROJECT
apm.yml must never masquerade as user/policy to skip the gate.  The source
label is assigned by discover_scripts from WHICH file declared the entry --
the entry's own content cannot override it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import build_runner_from_context, discover_scripts


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.usefixtures("hermetic_sources")
def test_project_entry_cannot_self_declare_source(apm_home: Path, tmp_path: Path) -> None:
    """An in-entry `source: user` is ignored; the entry stays source=project."""
    project = tmp_path / "project"
    _write(
        project / "apm.yml",
        "lifecycle:\n  post-install:\n"
        "    - type: command\n      bash: echo evil\n      source: user\n",
    )
    scripts = discover_scripts(project_root=str(project))
    assert scripts, "expected the project entry to be discovered"
    assert all(s.source == "project" for s in scripts), (
        "project apm.yml entry forged its source tier"
    )


@pytest.mark.usefixtures("hermetic_sources")
def test_untrusted_project_dropped_but_user_kept(apm_home: Path, tmp_path: Path) -> None:
    """Project entry is gated (dropped untrusted); user-tier entry runs ungated."""
    _write(
        apm_home / "apm.yml",
        "lifecycle:\n  post-install:\n    - type: command\n      bash: echo user\n",
    )
    project = tmp_path / "project"
    _write(
        project / "apm.yml",
        "lifecycle:\n  post-install:\n    - type: command\n      bash: echo project\n",
    )

    runner = build_runner_from_context(project_root=str(project))
    kept_sources = {s.source for s in runner.scripts_for_event("post-install")}

    assert "user" in kept_sources, "user-tier script must run ungated by design"
    assert "project" not in kept_sources, "untrusted project script must be gated out"
    assert runner._skipped_project_scripts == 1
