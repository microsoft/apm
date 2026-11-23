"""Local-bundle hooks are re-checked against their recorded approval (#4027)."""

from __future__ import annotations

import json
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


_SCRIPT = ".claude/hooks/hookpkg/hooks/scripts/x.sh"


@pytest.mark.parametrize(
    ("command", "runs"),
    [
        ('"${CLAUDE_PROJECT_DIR}/.claude/hooks/hookpkg/hooks/scripts/x.sh"', True),
        ('sh "$(pwd)/.claude/hooks/hookpkg/hooks/scripts/./x.sh"', True),
        ("cd -- .claude/hooks/hookpkg/hooks && sh scripts/./x.sh", True),
        ("bash -c 'cd .claude/hooks/hookpkg/hooks/scripts && ./x.sh'", True),
        ("sh 'unterminated x.sh", True),
        ('"${CLAUDE_PROJECT_DIR}/.claude/hooks/otherpkg/hooks/scripts/x.sh"', False),
        ('cd "$HOME" && sh /opt/tools/x.sh', False),
        ("cd . && " * 20 + "true", False),
        (" && ".join(f"cd d{index}" for index in range(12)) + " && true", False),
        ("./tools/lint.sh", False),
        ("sh $(pwd)/.claude/hooks/hookpkg/hooks/scripts/./x.sh", True),
        ("sh `pwd`/.claude/hooks/hookpkg/hooks/scripts/./x.sh", True),
        ("(cd / && true); sh .claude/hooks/hookpkg/hooks/scripts/./x.sh", True),
        ("cd .claude/hooks/hookpkg/hooks && sh -c 'sh scripts/./x.sh'", True),
    ],
    ids=[
        "project-dir-anchor",
        "command-substitution",
        "cd-terminator",
        "nested-shell",
        "unparsable",
        "sibling-package",
        "same-name-absolute",
        "many-cd",
        "distinct-cd",
        "unrelated",
        "unquoted-substitution",
        "backtick-substitution",
        "subshell-cd",
        "nested-inherits-cd",
    ],
)
def test_script_references_resolve_under_any_spelling(command: str, runs: bool) -> None:
    """#5106: a hook command runs a bundle script exactly when it may name it."""
    from apm_cli.install.local_bundle_hook_trust import _runs_script, _script_refs

    root = Path("/project")
    refs = _script_refs(root, [_SCRIPT])

    assert _runs_script(command, refs, (root, root / ".claude"), root) is runs


def test_script_references_keep_a_quoted_root_with_spaces_and_a_drive_path() -> None:
    from apm_cli.install.local_bundle_hook_trust import _runs_script, _script_refs

    spaced = Path("/tmp/my project")
    assert _runs_script(
        f'sh "{spaced.as_posix()}/{_SCRIPT}"', _script_refs(spaced, [_SCRIPT]), (spaced,), spaced
    )
    drive = "C:/project"
    refs = (frozenset({"x.sh"}), frozenset({f"c:/project/{_SCRIPT}"}))
    assert _runs_script(f"{drive}/.claude/hooks/hookpkg/hooks/scripts/./x.sh", refs, (), Path("/"))
    assert _runs_script(
        f"sh {drive}/.claude/hooks/hookpkg/hooks/scripts/./X.SH", refs, (), Path("/")
    )
