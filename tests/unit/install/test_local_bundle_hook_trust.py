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


_COPILOT_SCRIPT = ".github/hooks/scripts/hookpkg/hooks/scripts/x.sh"


def _user_hook_with_cwd(project: Path, command: str, cwd: object, prepare=None) -> set[str]:
    """Write an unowned Copilot hook running *command* in *cwd*; return the hook files in use."""
    from apm_cli.install.local_bundle_hook_trust import _per_file_configs_running
    from apm_cli.integration.targets import KNOWN_TARGETS

    (project / _COPILOT_SCRIPT).parent.mkdir(parents=True)
    (project / _COPILOT_SCRIPT).write_bytes(b"#!/bin/sh\n")
    if prepare is not None:
        prepare(project)
    handler = {"type": "command", "bash": command, "cwd": cwd}
    hook = project / ".github" / "hooks" / "user.json"
    hook.write_text(json.dumps({"version": 1, "hooks": {"preToolUse": [handler]}}))
    return _per_file_configs_running(project, [KNOWN_TARGETS["copilot"]], set(), {_COPILOT_SCRIPT})


def test_a_handler_running_a_bundle_script_through_its_cwd_is_in_use(tmp_path: Path) -> None:
    """#5281: ``./x.sh`` in the script's own directory runs the bundle's script."""
    assert _user_hook_with_cwd(
        tmp_path, "./x.sh", ".github/hooks/scripts/hookpkg/hooks/scripts"
    ) == {".github/hooks/user.json"}


def test_a_handler_whose_cwd_points_elsewhere_is_not_in_use(tmp_path: Path) -> None:
    """#5281 control: a script path in an unrelated directory runs another script."""
    assert _user_hook_with_cwd(tmp_path, "./scripts/x.sh", "tools") == set()


def test_a_handler_whose_cwd_cannot_be_resolved_inside_the_project_is_in_use(
    tmp_path: Path,
) -> None:
    """#5281: an escaping, variable or non-string cwd cannot be cleared; fail closed."""
    for index, cwd in enumerate(
        ("../outside", "$HOME/hooks", "%USERPROFILE%/hooks", "~/hooks", 7, "")
    ):
        project = tmp_path / f"p{index}"
        assert _user_hook_with_cwd(project, "./x.sh", cwd) == {".github/hooks/user.json"}, cwd


_SCRIPT_DIR = _COPILOT_SCRIPT.rsplit("/", 1)[0]


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


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        ("./x.sh", "tools"),
        ("sh -e ./x.sh", "tools"),
        ("bash -eu ./x.sh", "tools"),
        ("./prefix-x.sh", _SCRIPT_DIR),
        ("./scripts/x.sh", _SCRIPT_DIR),
    ],
    ids=[
        "bare-name-elsewhere",
        "shell-options-elsewhere",
        "shell-flags-elsewhere",
        "longer-name-in-script-dir",
        "subdir-of-script-dir",
    ],
)
def test_a_cwd_handler_running_another_script_is_not_in_use(
    tmp_path: Path, command: str, cwd: str
) -> None:
    """#5332: the command resolves from its declared cwd, as a whole path, not as a substring."""
    assert _user_hook_with_cwd(tmp_path, command, cwd) == set()


@pytest.mark.parametrize(
    ("command", "alias"),
    [
        ("./x.sh", "x.sh"),
        ("sh -e ./x.sh", "x.sh"),
        ("./lk/x.sh", "lk"),
    ],
    ids=["linked-script", "linked-script-shell-options", "linked-directory"],
)
def test_a_cwd_operand_through_a_link_to_the_bundle_script_is_in_use(
    tmp_path: Path, command: str, alias: str
) -> None:
    """A link under the handler's cwd may alias the bundle script: never cleared lexically."""

    def link(project: Path) -> None:
        tools = project / "tools"
        tools.mkdir()
        script = project / _COPILOT_SCRIPT
        target = script if alias == "x.sh" else script.parent
        (tools / alias).symlink_to(target, target_is_directory=alias != "x.sh")

    assert _user_hook_with_cwd(tmp_path, command, "tools", link) == {".github/hooks/user.json"}


def test_a_linked_cwd_is_in_use_whatever_the_name(tmp_path: Path) -> None:
    """A linked cwd may alias the script directory, so it is never cleared (fail closed)."""

    def link(project: Path) -> None:
        (project / "lk").symlink_to(project / _SCRIPT_DIR, target_is_directory=True)

    assert _user_hook_with_cwd(tmp_path, "./prefix-x.sh", "lk", link) == {".github/hooks/user.json"}


def _user_hook(project: Path, command: str, prepare) -> set[str]:
    """Write an unowned Copilot hook running *command* with no cwd; return the hook files in use."""
    from apm_cli.install.local_bundle_hook_trust import _per_file_configs_running
    from apm_cli.integration.targets import KNOWN_TARGETS

    (project / _COPILOT_SCRIPT).parent.mkdir(parents=True)
    (project / _COPILOT_SCRIPT).write_bytes(b"#!/bin/sh\n")
    prepare(project)
    hook = project / ".github" / "hooks" / "user.json"
    handler = {"type": "command", "bash": command}
    hook.write_text(json.dumps({"version": 1, "hooks": {"preToolUse": [handler]}}))
    return _per_file_configs_running(project, [KNOWN_TARGETS["copilot"]], set(), {_COPILOT_SCRIPT})


def _linked_leaf(project: Path) -> None:
    (project / "tools").mkdir()
    (project / "tools" / "x.sh").symlink_to(project / _COPILOT_SCRIPT)


def _linked_parent(project: Path) -> None:
    (project / "tools").symlink_to(project / _SCRIPT_DIR, target_is_directory=True)


def _plain_copy(project: Path) -> None:
    (project / "tools").mkdir()
    (project / "tools" / "x.sh").write_bytes(b"#!/bin/sh\n")


@pytest.mark.parametrize(
    ("command", "prepare", "in_use"),
    [
        ("sh tools/x.sh", _linked_leaf, True),
        ("sh tools/x.sh", _linked_parent, True),
        ("sh {root}/tools/x.sh", _linked_leaf, True),
        ("sh tools/x.sh", _plain_copy, False),
    ],
    ids=["linked-leaf", "linked-parent", "absolute-through-link", "plain-control"],
)
def test_an_operand_through_a_link_is_in_use(
    tmp_path: Path, command: str, prepare, in_use: bool
) -> None:
    """A link on the operand path may alias the bundle script: in use; a plain file is not."""
    found = _user_hook(tmp_path, command.format(root=tmp_path.as_posix()), prepare)

    assert found == ({".github/hooks/user.json"} if in_use else set())


@pytest.mark.parametrize(
    "command",
    ["x.sh", "sh x.sh", "cd .. && ./scripts/x.sh", "./$NAME", "./../scripts/x.sh"],
    ids=["path-lookup", "bare-argument", "moves", "expansion", "climbs"],
)
def test_a_cwd_handler_that_may_still_run_the_script_is_in_use(
    tmp_path: Path, command: str
) -> None:
    """#5332 fail closed: a known cwd clears only a command that certainly resolves elsewhere."""
    assert _user_hook_with_cwd(tmp_path, command, _SCRIPT_DIR + "/sub") == {
        ".github/hooks/user.json"
    }


def test_a_known_cwd_does_not_clear_a_sibling_handler_without_one(tmp_path: Path) -> None:
    """#5332: a handler without a cwd keeps the default bases and default-deny."""
    from apm_cli.install.local_bundle_hook_trust import _per_file_configs_running
    from apm_cli.integration.targets import KNOWN_TARGETS

    (tmp_path / _COPILOT_SCRIPT).parent.mkdir(parents=True)
    (tmp_path / _COPILOT_SCRIPT).write_bytes(b"#!/bin/sh\n")
    handlers = [
        {"type": "command", "bash": "./x.sh", "cwd": "tools"},
        {"type": "command", "bash": "./x.sh"},
    ]
    hook = tmp_path / ".github" / "hooks" / "user.json"
    hook.write_text(json.dumps({"version": 1, "hooks": {"preToolUse": handlers}}))

    found = _per_file_configs_running(
        tmp_path, [KNOWN_TARGETS["copilot"]], set(), {_COPILOT_SCRIPT}
    )

    assert found == {".github/hooks/user.json"}


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
        ("sh .claude/hooks/hookpkg/hooks/scripts/./'x'.sh", True),
        ('sh .claude/hooks/hookpkg/hooks/scripts/"x"".sh"', True),
        ("sh .claude/hooks/hookpkg/hooks/scripts/x\\.sh", True),
        ("sh .claude/hooks/hookpkg/hooks/scripts/$'\\x78'.sh", True),
        ('sh -e "/opt/other .claude/hooks/hookpkg/hooks/scripts/./x.sh"', False),
        ('bash -eu "/opt/other .claude/hooks/hookpkg/hooks/scripts/./x.sh"', False),
        ('sh -o errexit -x "/opt/other .claude/hooks/hookpkg/hooks/scripts/./x.sh"', False),
        ('/bin/bash -e "/opt/other .claude/hooks/hookpkg/hooks/scripts/./x.sh" arg', False),
        ("sh -c 'sh -e \"/opt/other .claude/hooks/hookpkg/hooks/scripts/./x.sh\"'", False),
        ("bash -e .claude/hooks/hookpkg/hooks/scripts/./x.sh", True),
        ('sh -eu "/project/.claude/hooks/hookpkg/hooks/scripts/./x.sh"', True),
        ('sh -Z "/opt/other .claude/hooks/hookpkg/hooks/scripts/./x.sh"', True),
        ('sh -s "/opt/other .claude/hooks/hookpkg/hooks/scripts/./x.sh"', True),
        ('bash --rcfile "/opt/other .claude/hooks/hookpkg/hooks/scripts/./x.sh"', True),
        (
            'sh -e "/opt/other .claude/hooks/hookpkg/hooks/scripts/./x.sh" && sh .claude/hooks/hookpkg/hooks/scripts/x.sh',
            True,
        ),
        ("echo .claude/hooks/hookpkg/hooks/scripts/x.sh", True),
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
        "quoted-fragment",
        "adjacent-quoted-fragments",
        "escaped-dot",
        "ansi-c-quoting",
        "shell-option-unrelated",
        "shell-combined-options-unrelated",
        "shell-o-option-unrelated",
        "shell-path-option-unrelated",
        "shell-c-then-option-unrelated",
        "shell-option-bundle-script",
        "shell-option-bundle-absolute",
        "shell-unknown-option",
        "shell-stdin-option",
        "shell-long-option",
        "shell-option-then-bundle-script",
        "mention-without-run",
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
    # An option hands quoted words to a rescan, which must not split a spaced root.
    assert runs_script(
        f'sh -e "{spaced.as_posix()}/{_SCRIPT.replace("/scripts/", "/scripts/./")}"',
        script_refs(spaced, [_SCRIPT]),
        (spaced,),
        spaced,
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


_DIR = ".claude/hooks/hookpkg/hooks/scripts"


@pytest.mark.parametrize(
    "command",
    [
        f"S=x; sh {_DIR}/${{S}}.sh",
        f"S=x; sh {_DIR}/$S.sh",
        f'sh "$(printf {_DIR}/%s.sh x)"',
        f"sh `printf {_DIR}/%s.sh x`",
        f"sh {_DIR}/x.{{sh,}}",
        f"sh {_DIR}/x.s?",
        f"sh {_DIR}/*.sh",
        f"sh {_DIR}/[x].sh",
        f"sh {_DIR}/x.\\\nsh",
        f'a=x; b=.sh; eval "sh {_DIR}/$a$b"',
        f"set RUN=x&& sh {_DIR}/%RUN%.sh",
        f"set RUN=x&& sh {_DIR}/!RUN!.sh",
        f"sh /opt/tools/x.sh; S=x; sh {_DIR}/$S.sh",
    ],
    ids=[
        "braced-variable",
        "variable",
        "command-substitution",
        "backticks",
        "brace-list",
        "glob-question",
        "glob-star",
        "glob-class",
        "backslash-newline",
        "eval-variables",
        "cmd-percent-variable",
        "cmd-delayed-variable",
        "hidden-beside-unrelated-same-name",
    ],
)
def test_a_spelling_that_hides_the_script_name_fails_closed(command: str) -> None:
    """#5106: an expansion left after the anchors may spell the script, so it runs it."""
    from apm_cli.install.hook_script_refs import runs_script, script_refs

    root = Path("/project")

    assert runs_script(command, script_refs(root, [_SCRIPT]), (root, root / ".claude"), root)


@pytest.mark.parametrize(
    "command",
    [
        '"${CLAUDE_PROJECT_DIR}/.claude/hooks/otherpkg/hooks/scripts/lint.sh"',
        '"$CLAUDE_PROJECT_DIR/.claude/hooks/otherpkg/hooks/scripts/lint.sh"',
        'pwsh -File "$env:CLAUDE_PROJECT_DIR/.claude/hooks/otherpkg/run.ps1"',
        'sh "$HOME/tools/lint.sh"',
        'sh "${HOME}/tools/lint.sh"',
        'pwsh -File "$env:HOME/tools/run.ps1"',
        "date +%Y && sh tools/lint.sh",
        "date +%Y-%m-%d && sh tools/lint.sh",
    ],
    ids=[
        "braced-project-dir",
        "project-dir",
        "env-project-dir",
        "home",
        "braced-home",
        "env-home",
        "date-format",
        "date-format-fields",
    ],
)
def test_a_lone_documented_anchor_never_refuses(command: str) -> None:
    """#5106: anchors are substituted first and a date format names no variable, so a hook
    running another script never refuses."""
    from apm_cli.install.hook_script_refs import runs_script, script_refs

    root = Path("/project")

    assert not runs_script(command, script_refs(root, [_SCRIPT]), (root, root / ".claude"), root)


def test_the_audit_reads_each_hook_config_and_record_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5211: the local-bundle-hooks audit reads every config, sidecar and record once."""
    from collections import Counter

    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.policy.ci_checks import _check_local_bundle_hooks

    root, _targets = _project(tmp_path, local_bundle_hook_source_marker("hookpkg", _KEY))
    record = {"_apm_source": local_bundle_hook_source_marker("hookpkg", _KEY), "hook_files": []}
    (root / ".github").mkdir()
    (root / ".github" / "apm-hooks.json").write_text(json.dumps({"local-bundle-hooks": [record]}))
    reads: Counter[str] = Counter()
    real_read_text = Path.read_text

    def counting(self: Path, *args, **kwargs):
        reads[self.relative_to(root).as_posix() if self.is_relative_to(root) else str(self)] += 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    # A merged target's sidecar is read as a regular file only (#5328).
    from apm_cli.integration import hook_ownership

    real_open_regular = hook_ownership.open_regular_file

    def counting_regular(path: Path, *args, **kwargs):
        reads[path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)] += 1
        return real_open_regular(path, *args, **kwargs)

    monkeypatch.setattr(hook_ownership, "open_regular_file", counting_regular)
    targets = [KNOWN_TARGETS["claude"], KNOWN_TARGETS["copilot"]]

    result = _check_local_bundle_hooks(
        root,
        LockFile(),
        APMPackage(name="p", version="1.0.0"),
        targets,
        user_scope=False,
        no_policy=True,
    )

    assert result.name == "local-bundle-hooks"
    read = {path: count for path, count in reads.items() if path.endswith(".json")}
    assert read[".claude/settings.json"] == 1, read
    assert read[".claude/apm-hooks.json"] == 1, read
    assert read[".github/apm-hooks.json"] == 1, read
    assert max(read.values()) == 1, read
