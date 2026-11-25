"""``apm lock`` never adopts another user's ancestor apm.yml.

The shared ``_find_apm_yml`` walk (git's ``safe.directory`` class) skips an
ancestor manifest the effective user does not own: ``/tmp/work`` must not
lock against another user's planted ``/tmp/apm.yml`` (``apm update`` shares
the same walk).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-install-command-scope-selection"),
]


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership")
def test_lock_never_adopts_an_ancestor_apm_yml_another_user_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared"
    work = shared / "work"
    work.mkdir(parents=True)
    planted = shared / "apm.yml"
    planted.write_text("name: planted\nversion: 0.0.1\n", encoding="utf-8")
    owner = planted.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    monkeypatch.chdir(work)

    result = CliRunner().invoke(cli, ["lock"])

    output = " ".join(result.output.split())
    assert result.exit_code == 1, output
    assert f"Skipping {planted}: owned by uid {owner}" in output, output
    assert "No apm.yml found" in output, output
    assert sorted(path.name for path in shared.iterdir()) == ["apm.yml", "work"]


# -- restored from #4576's test_project_root_discovery.py (owner cells) --


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership")
def test_an_ancestor_apm_yml_another_user_owns_is_skipped_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """/tmp/work never adopts another user's /tmp/apm.yml (git's safe.directory class)."""
    from apm_cli.commands._helpers import _find_apm_yml

    shared = tmp_path / "shared"
    work = shared / "work" / "src"
    work.mkdir(parents=True)
    planted = shared / "apm.yml"
    planted.write_text("name: planted\nversion: 0.0.1\n", encoding="utf-8")
    monkeypatch.setattr(os, "geteuid", lambda: planted.stat().st_uid + 1)

    assert _find_apm_yml(work) is None
    out = " ".join(capsys.readouterr().out.split())
    assert str(planted) in out, out
    assert f"owned by uid {planted.stat().st_uid}" in out, out


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership")
def test_an_ancestor_apm_yml_the_user_owns_is_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from apm_cli.commands._helpers import _find_apm_yml

    work = tmp_path / "project" / "src"
    work.mkdir(parents=True)
    manifest = tmp_path / "project" / "apm.yml"
    manifest.write_text("name: mine\nversion: 0.0.1\n", encoding="utf-8")

    assert _find_apm_yml(work) == manifest.resolve()
    assert "Skipping" not in capsys.readouterr().out


# -- only a regular file (or a link to one) is a manifest --


@pytest.mark.parametrize("shape", ["directory", "dangling-link"])
def test_an_apm_yml_that_is_not_a_regular_file_is_passed_over(
    tmp_path: Path, shape: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from apm_cli.commands._helpers import _find_apm_yml

    manifest = tmp_path / "apm.yml"
    manifest.write_text("name: mine\nversion: 0.0.1\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    if shape == "directory":
        (work / "apm.yml").mkdir()
    else:
        try:
            (work / "apm.yml").symlink_to(work / "missing.yml")
        except OSError:
            pytest.skip("symlinks unavailable")

    assert _find_apm_yml(work) == manifest.resolve()
    assert "Skipping" not in capsys.readouterr().out
