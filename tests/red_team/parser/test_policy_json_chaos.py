"""RED-TEAM: admin policy.d JSON drop-in chaos.

The admin tier loads ``*.json`` files from the policy directory, each
requiring ``{version: 1, scripts: {...}}``. The loader must skip malformed
or wrong-shaped files silently, load surviving files in sorted name order,
treat an event present in two files additively, ignore a directory that
merely ends in ``.json``, follow a symlinked JSON file, and stay bounded
on an enormous file -- never crashing discovery.

All policy I/O is redirected to a tmp dir via the ``policy_dir`` fixture,
so the real ``/etc/apm/policy.d`` is never read.
"""

from __future__ import annotations

import json

from .conftest import run_guarded


def _write_json(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def _parse(path):
    from apm_cli.core.lifecycle_scripts import parse_script_file

    return parse_script_file(path, "policy")


def test_malformed_json_is_skipped(policy_dir):
    bad = policy_dir / "00_broken.json"
    bad.write_text("{ not valid json ]", encoding="utf-8")
    assert _parse(bad) == []


def test_wrong_version_is_skipped(policy_dir):
    f = _write_json(
        policy_dir / "01.json",
        {"version": 2, "scripts": {"post-install": [{"type": "command", "bash": "echo x"}]}},
    )
    assert _parse(f) == []


def test_missing_version_is_skipped(policy_dir):
    f = _write_json(
        policy_dir / "01.json",
        {"scripts": {"post-install": [{"type": "command", "bash": "echo x"}]}},
    )
    assert _parse(f) == []


def test_scripts_not_a_dict_is_skipped(policy_dir):
    f = _write_json(policy_dir / "02.json", {"version": 1, "scripts": "not-a-dict"})
    assert _parse(f) == []


def test_scripts_list_value_not_a_list_is_skipped(policy_dir):
    f = _write_json(
        policy_dir / "03.json",
        {"version": 1, "scripts": {"post-install": {"type": "command"}}},
    )
    assert _parse(f) == []


def test_sorted_load_order_and_additive(policy_dir, tmp_path, isolated_home):
    _write_json(
        policy_dir / "20_b.json",
        {"version": 1, "scripts": {"post-install": [{"type": "command", "bash": "echo B"}]}},
    )
    _write_json(
        policy_dir / "10_a.json",
        {"version": 1, "scripts": {"post-install": [{"type": "command", "bash": "echo A"}]}},
    )
    from apm_cli.core.lifecycle_scripts import discover_scripts

    project = tmp_path / "proj"
    project.mkdir()
    entries = discover_scripts(project_root=str(project))
    bashes = [e.bash for e in entries if e.source == "policy"]
    # Sorted by filename: 10_a before 20_b.
    assert bashes == ["echo A", "echo B"]


def test_event_in_two_files_is_additive(policy_dir, tmp_path, isolated_home):
    _write_json(
        policy_dir / "a.json",
        {"version": 1, "scripts": {"post-install": [{"type": "command", "bash": "echo one"}]}},
    )
    _write_json(
        policy_dir / "b.json",
        {"version": 1, "scripts": {"post-install": [{"type": "command", "bash": "echo two"}]}},
    )
    from apm_cli.core.lifecycle_scripts import discover_scripts

    project = tmp_path / "proj"
    project.mkdir()
    entries = [e for e in discover_scripts(project_root=str(project)) if e.source == "policy"]
    assert len(entries) == 2
    assert {e.bash for e in entries} == {"echo one", "echo two"}


def test_directory_named_json_is_ignored(policy_dir, tmp_path, isolated_home):
    # A directory whose name ends in .json must not be treated as a file.
    (policy_dir / "evil.json").mkdir()
    _write_json(
        policy_dir / "ok.json",
        {"version": 1, "scripts": {"post-install": [{"type": "command", "bash": "echo ok"}]}},
    )
    from apm_cli.core.lifecycle_scripts import discover_scripts

    project = tmp_path / "proj"
    project.mkdir()
    entries = [e for e in discover_scripts(project_root=str(project)) if e.source == "policy"]
    assert [e.bash for e in entries] == ["echo ok"]


def test_symlinked_json_is_followed(policy_dir, tmp_path, isolated_home):
    target = tmp_path / "real_policy.json"
    _write_json(
        target,
        {"version": 1, "scripts": {"post-install": [{"type": "command", "bash": "echo linked"}]}},
    )
    link = policy_dir / "link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        import pytest

        pytest.skip("symlinks not supported on this platform")
    from apm_cli.core.lifecycle_scripts import discover_scripts

    project = tmp_path / "proj"
    project.mkdir()
    entries = [e for e in discover_scripts(project_root=str(project)) if e.source == "policy"]
    assert [e.bash for e in entries] == ["echo linked"]


def test_enormous_policy_file_is_bounded(policy_dir):
    big = {
        "version": 1,
        "scripts": {
            "post-install": [{"type": "command", "bash": f"echo {i}"} for i in range(8000)]
        },
    }
    f = _write_json(policy_dir / "big.json", big)
    finished, result, exc = run_guarded(lambda: _parse(f), timeout=20.0)
    assert finished, "parsing an enormous policy file did not finish in time"
    assert exc is None, f"enormous policy file raised: {exc!r}"
    assert len(result) == 8000
