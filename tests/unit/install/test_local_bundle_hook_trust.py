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
        ("sh $(pwd)/.claude/hooks/hookpkg/hooks/scripts/./x.sh; true", True),
        ("sh `pwd`/.claude/hooks/hookpkg/hooks/scripts/./x.sh; true", True),
        ("(cd / && true); sh .claude/hooks/hookpkg/hooks/scripts/./x.sh", True),
        ("cd .claude/hooks/hookpkg/hooks && sh -c 'sh scripts/./x.sh'", True),
        (
            'CLAUDE_PROJECT_DIR="$PWD/.claude/hooks/hookpkg/hooks"; '
            'sh "$CLAUDE_PROJECT_DIR/scripts/./x.sh"',
            True,
        ),
        ('HOME="$PWD/.claude/hooks/hookpkg/hooks"; sh "$HOME/scripts/./x.sh"', True),
        ("cd - && sh scripts/./x.sh", True),
        ("sh -c '.claude/hooks/hookpkg/hooks/scripts/./x.sh;true'", True),
        ("sh .claude/hooks/hookpkg/hooks/script*/./x.sh", True),
        ("sh ././x.sh", True),
        ('script=.claude/hooks/hookpkg/hooks/scripts/./x.sh; sh "$script"', True),
        ('"${CLAUDE_PROJECT_DIR}/.claude/hooks/otherpkg/hooks/scripts/x.sh"', False),
        ("bash .github/hooks/otherpkg/scripts/x.sh", False),
        ('cd "$HOME" && sh /opt/tools/x.sh', False),
        ('sh "/opt/other .claude/hooks/hookpkg/hooks/scripts/./x.sh"', False),
        ("cd . && " * 20 + "true", False),
        ("./tools/lint.sh", False),
        ("./tools/max.sh", False),
        ("sh -c 'sh .claude/hooks/hookpkg/hooks/scripts/./x.sh'", True),
        ('cmd /c "sh .claude/hooks/hookpkg/hooks/scripts/./x.sh"', True),
        ("cd .claude && sh -c 'sh hooks/otherpkg/../hookpkg/hooks/scripts/x.sh'", True),
        ("sh hooks/hookpkg/hooks/scripts/./x.sh", True),
    ],
    ids=[
        "project-dir-anchor",
        "command-substitution",
        "cd-terminator",
        "nested-shell",
        "unparsable",
        "substitution-then-separator",
        "backtick-then-separator",
        "subshell-cd",
        "nested-inherits-cd",
        "reassigned-project-dir",
        "reassigned-home",
        "cd-dash",
        "nested-without-space",
        "glob-directory",
        "repeated-dot",
        "assigned-script",
        "sibling-package",
        "sibling-relative",
        "same-name-absolute",
        "quoted-absolute-with-space",
        "many-cd",
        "unrelated",
        "longer-name",
        "nested-with-space",
        "cmd-slash-c",
        "nested-inherits-cd-with-space",
        "target-root-relative",
    ],
)
def test_script_references_resolve_under_any_spelling(command: str, runs: bool) -> None:
    """#5106: a hook command runs a bundle script unless it certainly names another file."""
    from apm_cli.install.hook_script_refs import runs_script, script_refs

    root = Path("/project")
    refs = script_refs(root, [_SCRIPT])

    assert runs_script(command, refs, (root, root / ".claude"), root) is runs


def test_script_references_keep_a_quoted_root_with_spaces_and_a_drive_path() -> None:
    from apm_cli.install.hook_script_refs import runs_script, script_refs

    spaced = Path("/tmp/my project")
    assert runs_script(
        f'sh "{spaced.as_posix()}/{_SCRIPT}"', script_refs(spaced, [_SCRIPT]), (spaced,), spaced
    )
    refs = (frozenset({"x.sh"}), frozenset({f"c:/project/{_SCRIPT}"}))
    assert runs_script(
        "C:/project/.claude/hooks/hookpkg/hooks/scripts/./x.sh", refs, (Path("/"),), Path("/")
    )
    assert runs_script(
        "sh C:/project/.claude/hooks/hookpkg/hooks/scripts/./X.SH", refs, (Path("/"),), Path("/")
    )


@pytest.mark.parametrize(
    ("script", "command"),
    [
        (
            ".claude/hooks/hookpkg/hooks/scripts/run.sh",
            "sh .claude/hooks/hookpkg/hooks/scripts/Run.sh",
        ),
        (
            ".claude/hooks/hookpkg/hooks/scripts/run.sh",
            "sh .claude/hooks/HookPkg/hooks/scripts/Run.sh",
        ),
        (
            ".claude/hooks/hookpkg/hooks/scripts/run.sh",
            "sh /project/.CLAUDE/hooks/hookpkg/hooks/scripts/RUN.SH",
        ),
        (
            ".claude/hooks/hookpkg/hooks/scripts/caf\u00e9.sh",
            "sh .claude/hooks/hookpkg/hooks/scripts/cafe\u0301.sh",
        ),
        (
            ".claude/hooks/hookpkg/hooks/scripts/caf\u00e9.sh",
            "sh .claude/hooks/hookpkg/hooks/scripts/CAFE\u0301.SH",
        ),
        (
            ".claude/hooks/r\u00e9sum\u00e9/hooks/scripts/run.sh",
            "sh .claude/hooks/re\u0301sume\u0301/hooks/scripts/run.sh",
        ),
    ],
    ids=[
        "case-name",
        "case-directory-and-name",
        "case-absolute",
        "nfd-name",
        "nfd-and-case-name",
        "nfd-directory",
    ],
)
def test_script_references_compare_by_the_shared_caseless_key(script: str, command: str) -> None:
    """#5106: a case-insensitive volume runs any case or NFD spelling of the script.

    Comparing by the one caseless key on every OS over-refuses on a
    case-sensitive volume, which fails closed.
    """
    from apm_cli.install.hook_script_refs import runs_script, script_refs

    root = Path("/project")

    assert runs_script(command, script_refs(root, [script]), (root, root / ".claude"), root)
