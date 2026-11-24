"""Local-bundle hooks are re-checked against their recorded approval (#4027)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.local_bundle_hook_trust import (
    REASON_NOT_GRANTED,
    REASON_UNRECORDED,
    unadmitted_bundle_hooks,
)
from apm_cli.integration.hook_ownership import (
    LocalBundleMarkers,
    local_bundle_hook_source_marker,
    parse_local_bundle_hook_source,
)

_KEY = "hookpkg#1.0.0@sha256:" + "a" * 64


def _gate_off():
    from apm_cli.security.executables import AUTHORIZED_GATE_DISABLED, authorized_exec_map

    return authorized_exec_map(AUTHORIZED_GATE_DISABLED)


def test_marker_round_trips_the_alias_slug_and_real_key() -> None:
    marker = local_bundle_hook_source_marker("team-hooks", _KEY)
    assert parse_local_bundle_hook_source(marker) == ("team-hooks", _KEY)
    assert parse_local_bundle_hook_source(local_bundle_hook_source_marker("x")) == ("x", None)
    assert parse_local_bundle_hook_source("owner/repo") is None


def test_slug_markers_match_every_recorded_key_of_that_slug_only() -> None:
    markers = LocalBundleMarkers("hookpkg")
    assert local_bundle_hook_source_marker("hookpkg", _KEY) in markers
    assert local_bundle_hook_source_marker("hookpkg") in markers
    assert local_bundle_hook_source_marker("hookpkg-two", _KEY) not in markers


def _project(tmp_path: Path, marker: str) -> tuple[Path, list]:
    claude = tmp_path / ".claude"
    claude.mkdir(parents=True)
    entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}
    (claude / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [entry]}}))
    (claude / "apm-hooks.json").write_text(
        json.dumps({"PreToolUse": [{**entry, "_apm_source": marker}]})
    )
    return tmp_path, [SimpleNamespace(name="claude", root_dir=".claude")]


def test_granted_key_is_admitted_and_gate_off_admits_all(tmp_path: Path) -> None:
    root, targets = _project(tmp_path, local_bundle_hook_source_marker("hookpkg", _KEY))
    assert unadmitted_bundle_hooks(root, targets, lambda: {_KEY: {"hooks": True}}) == []
    assert unadmitted_bundle_hooks(root, targets, _gate_off) == []
    # A missing map is not a gate-off map: it admits nothing.
    (missing,) = unadmitted_bundle_hooks(root, targets, lambda: None)
    assert missing.reason == REASON_NOT_GRANTED


def test_revoked_or_unrecorded_key_is_not_admitted(tmp_path: Path) -> None:
    root, targets = _project(tmp_path, local_bundle_hook_source_marker("hookpkg", _KEY))
    (item,) = unadmitted_bundle_hooks(root, targets, dict)
    assert (item.slug, item.reason) == ("hookpkg", REASON_NOT_GRANTED)

    legacy, legacy_targets = _project(
        tmp_path / "legacy", local_bundle_hook_source_marker("hookpkg")
    )
    (item,) = unadmitted_bundle_hooks(legacy, legacy_targets, lambda: {_KEY: {"hooks": True}})
    assert item.reason == REASON_UNRECORDED


def test_no_deployed_bundle_hooks_never_resolves_trust(tmp_path: Path) -> None:
    calls: list[int] = []
    targets = [SimpleNamespace(name="claude", root_dir=".claude")]
    assert unadmitted_bundle_hooks(tmp_path, targets, lambda: calls.append(1)) == []
    assert calls == []


_SCRIPT = ".github/hooks/scripts/hookpkg/hooks/scripts/x.sh"


def _user_hook_with_cwd(project: Path, command: str, cwd: object, prepare=None) -> set[str]:
    """Write an unowned Copilot hook running *command* in *cwd*; return the hook files in use."""
    from apm_cli.install.local_bundle_hook_trust import _per_file_configs_running
    from apm_cli.integration.targets import KNOWN_TARGETS

    (project / _SCRIPT).parent.mkdir(parents=True)
    (project / _SCRIPT).write_bytes(b"#!/bin/sh\n")
    if prepare is not None:
        prepare(project)
    handler = {"type": "command", "bash": command, "cwd": cwd}
    hook = project / ".github" / "hooks" / "user.json"
    hook.write_text(json.dumps({"version": 1, "hooks": {"preToolUse": [handler]}}))
    return _per_file_configs_running(project, [KNOWN_TARGETS["copilot"]], set(), {_SCRIPT})


def test_a_handler_running_a_bundle_script_through_its_cwd_is_in_use(tmp_path: Path) -> None:
    """#5281: ``./x.sh`` in the script's own directory runs the bundle's script."""
    assert _user_hook_with_cwd(
        tmp_path, "./x.sh", ".github/hooks/scripts/hookpkg/hooks/scripts"
    ) == {".github/hooks/user.json"}


def test_a_handler_whose_cwd_points_elsewhere_is_not_in_use(tmp_path: Path) -> None:
    """#5281 control: the same command in an unrelated directory runs another script."""
    assert _user_hook_with_cwd(tmp_path, "./x.sh", "tools") == set()


def test_a_handler_whose_cwd_cannot_be_resolved_inside_the_project_is_in_use(
    tmp_path: Path,
) -> None:
    """#5281: an escaping, variable or non-string cwd cannot be cleared; fail closed."""
    for index, cwd in enumerate(
        ("../outside", "$HOME/hooks", "%USERPROFILE%/hooks", "~/hooks", 7, "")
    ):
        project = tmp_path / f"p{index}"
        assert _user_hook_with_cwd(project, "./x.sh", cwd) == {".github/hooks/user.json"}, cwd


_SCRIPT_DIR = _SCRIPT.rsplit("/", 1)[0]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_handler_whose_cwd_links_to_the_script_directory_is_in_use(tmp_path: Path) -> None:
    """#5296 review: a cwd that is a symlink to the script's directory runs it."""

    def link(project: Path) -> None:
        (project / "lk").symlink_to(project / _SCRIPT_DIR, target_is_directory=True)

    assert _user_hook_with_cwd(tmp_path, "./x.sh", "lk", link) == {".github/hooks/user.json"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_handler_whose_cwd_runs_through_a_symlinked_ancestor_is_in_use(tmp_path: Path) -> None:
    """#5296 review: ``lk -> .github/hooks`` makes ``lk/scripts/...`` the script's directory."""

    def link(project: Path) -> None:
        (project / "lk").symlink_to(project / ".github" / "hooks", target_is_directory=True)

    cwd = "lk/" + _SCRIPT_DIR.removeprefix(".github/hooks/")
    assert _user_hook_with_cwd(tmp_path, "./x.sh", cwd, link) == {".github/hooks/user.json"}


def test_a_handler_whose_cwd_differs_only_in_case_is_in_use_where_case_folds(
    tmp_path: Path,
) -> None:
    """#5296 review: on a case-insensitive filesystem ``.GITHUB/...`` is the same directory."""
    probe = tmp_path / "Probe"
    probe.mkdir()
    if not (tmp_path / "probe").exists():
        pytest.skip("case-sensitive filesystem")
    cwd = _SCRIPT_DIR.replace(".github", ".GITHUB")
    project = tmp_path / "p"
    assert _user_hook_with_cwd(project, "./x.sh", cwd) == {".github/hooks/user.json"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_handler_whose_cwd_links_outside_the_project_is_in_use(tmp_path: Path) -> None:
    """#5296 review: a cwd that escapes through a symlink cannot be cleared; fail closed."""
    outside = tmp_path / "outside"
    outside.mkdir()

    def link(project: Path) -> None:
        (project / "external").symlink_to(outside, target_is_directory=True)

    project = tmp_path / "p"
    assert _user_hook_with_cwd(project, "./x.sh", "external", link) == {".github/hooks/user.json"}


def test_a_handler_whose_cwd_holds_a_nul_is_in_use(tmp_path: Path) -> None:
    """#5296 review: an embedded NUL is no path; it refuses instead of crashing."""
    assert _user_hook_with_cwd(tmp_path, "./x.sh", "tools\x00bad") == {".github/hooks/user.json"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_handler_whose_cwd_climbs_out_of_a_link_is_in_use(tmp_path: Path) -> None:
    """#5296 review: ``lk/..`` follows the link before ``..``, landing in the script directory."""

    def link(project: Path) -> None:
        (project / _SCRIPT_DIR / "child").mkdir()
        (project / "lk").symlink_to(project / _SCRIPT_DIR / "child", target_is_directory=True)

    assert _user_hook_with_cwd(tmp_path, "./x.sh", "lk/..", link) == {".github/hooks/user.json"}
