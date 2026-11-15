"""Tests for the Targets and MCPs columns of ``apm deps list`` (#2976)."""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import itertools
import json
import os
import socket
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner, Result

from apm_cli.agent_plugins import MCP_SCHEMA_ID, PLUGIN_SCHEMA_ID
from apm_cli.cli import cli
from apm_cli.commands.deps._inventory import (
    NOTE_LEGACY,
    NOTE_MCP_SCOPE,
    NOTE_MCP_UNREADABLE,
    McpResult,
    TargetEvidence,
    TargetsResult,
    format_mcps,
    format_targets,
    package_mcp_count,
    package_targets,
    targets_legend,
)
from apm_cli.deps.lockfile import LockFile

# Source stays printable ASCII (repo encoding rule), so Rich's box characters
# are spelled as escapes.
BOX_VERTICAL = "\u2502"
RICH_TABLE_LINE_STARTS = "\u250f\u2503\u2521\u2502\u2514"  # top, header, rule, row, bottom
ELLIPSIS = "\u2026"

KNOWN, UNAVAILABLE = TargetEvidence.KNOWN, TargetEvidence.UNAVAILABLE
AMBIGUOUS, PARTIAL = TargetEvidence.AMBIGUOUS, TargetEvidence.PARTIAL

SKILLY, OTHER, THIRD = "./pkgs/skilly", "./pkgs/other", "./pkgs/third"
CLAUDE_SKILL = ".claude/skills/hello"
SHARED = ".agents/skills/hello"  # a directory several targets deploy into
GOOD_SERVER = {"name": "a", "registry": False, "transport": "http", "url": "http://x"}
STDIO_SERVER = {"name": "b", "registry": False, "transport": "stdio", "command": "run-b"}

Rows = list[dict[str, object]]


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _rows(*specs: tuple) -> Rows:
    """Build ledger rows from ``(target, value[, owners[, scope]])`` shorthand.

    A target written ``mcp:<runtime>`` makes an MCP service row for ``value``.
    Owners default to the skilly package, the scope to ``project``.
    """
    rows: Rows = []
    for target, value, *rest in specs:
        owners = list(rest[0]) if rest else [SKILLY]
        scope = rest[1] if len(rest) > 1 else "project"
        service = target.startswith("mcp:")
        rows.append(
            {
                "kind": "uri" if service else "project-relative",
                "target": "mcp" if service else target,
                "value": value,
                "runtime": target[4:] if service else None,
                "scope": scope,
                "owners": owners,
                "active_owner": owners[0],
                "content_hash": None,
            }
        )
    return rows


def _dep(key: str, **extra: object) -> dict[str, object]:
    name = key.rsplit("/", 1)[-1]
    return {
        "repo_url": f"_local/{name}",
        "name": name,
        "version": "1.0.0",
        "source": "local",
        "local_path": key,
        **extra,
    }


def _lock(rows: Rows | None = None, **skilly_extra: object) -> LockFile:
    """A lockfile of three local packages; ``rows=None`` omits the deployments key."""
    data: dict[str, object] = {
        "lockfile_version": "1",
        "dependencies": [_dep(SKILLY, **skilly_extra), _dep(OTHER), _dep(THIRD)],
    }
    if rows is not None:
        data["deployments"] = rows
    return LockFile.from_yaml(yaml.safe_dump(data, sort_keys=False))


def _manifest(pkg_dir: Path, mcp: list[object] | None = None, name: str = "pkg") -> Path:
    pkg_dir.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {"name": name, "version": "1.0.0", "description": "t"}
    if mcp is not None:
        data["dependencies"] = {"mcp": mcp}
    (pkg_dir / "apm.yml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return pkg_dir


def _manifest_text(root: Path, data: bytes | str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "apm.yml"
    (path.write_bytes if isinstance(data, bytes) else path.write_text)(data)
    return root


def _manifest_entry(root: Path, entry: dict[str, object]) -> Path:
    return _manifest(root, [entry])


def _agent_plugin(
    root: Path, servers: dict[str, object] | None = None, *, plugin_json: str | None = None
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    document = {"$schema": PLUGIN_SCHEMA_ID, "name": "native.plugin", "version": "1.0.0"}
    (root / "plugin.json").write_text(plugin_json or json.dumps(document), encoding="utf-8")
    if servers is not None:
        (root / "mcp.json").write_text(
            json.dumps({"$schema": MCP_SCHEMA_ID, "mcpServers": servers}), encoding="utf-8"
        )
    return root


def _plugin_mcp_document(root: Path, document: str) -> Path:
    _agent_plugin(root)
    (root / "mcp.json").write_text(document, encoding="utf-8")
    return root


def _plugin_mcp_directory(root: Path) -> Path:
    _agent_plugin(root)
    (root / "mcp.json").mkdir()
    return root


def _claude_plugin(root: Path) -> Path:
    from apm_cli.deps.plugin_parser import normalize_plugin_directory

    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "cp", "version": "1.0.0"}), encoding="utf-8")
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"srv": {"command": "run-srv"}}}), encoding="utf-8"
    )
    normalize_plugin_directory(root, manifest)
    return root


def _skill_package(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\nx\n")
    return root


def _hook_package(root: Path) -> Path:
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "hooks.json").write_text("{}")
    return root


def _empty_dir(root: Path) -> Path:
    root.mkdir()
    return root


def _unreadable_manifest(root: Path) -> Path:
    _manifest_text(root, "name: p\n")
    (root / "apm.yml").chmod(0)
    return root


def _unreadable_directory(root: Path) -> Path:
    _agent_plugin(root)
    root.chmod(0)
    return root


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    state = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            p = Path(dirpath) / name
            data = p.read_bytes() if p.is_file() else b""
            state[str(p)] = (hashlib.sha256(data).digest(), p.lstat().st_mtime_ns)
    return state


@contextlib.contextmanager
def _offline_and_unchanged(root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail on any network call and on any change under *root*."""

    def _blocked(*_a: object, **_k: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    before = _snapshot(root)
    yield
    assert _snapshot(root) == before


def _patch(monkeypatch: pytest.MonkeyPatch, dotted: str, replacement: object) -> None:
    """Patch ``module.attr``, resolving the module with importlib.

    ``apm_cli.commands.deps`` names a Click group, so pytest's dotted-string
    ``monkeypatch.setattr`` cannot walk to ``apm_cli.commands.deps._inventory``.
    """
    module_name, attr = dotted.rsplit(".", 1)
    monkeypatch.setattr(importlib.import_module(module_name), attr, replacement)


# ---------------------------------------------------------------------------
# package_targets
# ---------------------------------------------------------------------------

_MCP_SCOPE = {"mcp_count": 1, "has_primitives": True}
LEGACY_ONLY = _rows(("legacy", SHARED))
CLAUDE_AND_LEGACY = _rows(("claude", CLAUDE_SKILL), ("legacy", SHARED))
OWNED_BY_OTHER = _rows(("claude", ".claude/x", [OTHER]))

TARGET_CASES = [
    pytest.param(
        _rows(("claude", CLAUDE_SKILL), ("codex", SHARED), ("codex", SHARED + "/SKILL.md")),
        {},
        TargetsResult(KNOWN, ("claude", "codex")),
        id="the concrete targets named by the package's rows",
    ),
    pytest.param(
        OWNED_BY_OTHER,
        {},
        TargetsResult(KNOWN, ()),
        id="a package owning no rows in a recorded ledger has known empty targets",
    ),
    pytest.param(
        LEGACY_ONLY,
        {"has_primitives": True},
        TargetsResult(AMBIGUOUS),
        id="ambiguous when every row is unattributed, a shared-root row is never inferred from its directory",
    ),
    pytest.param(
        CLAUDE_AND_LEGACY,
        {"has_primitives": True},
        TargetsResult(PARTIAL, ("claude",), (NOTE_LEGACY,)),
        id="partial when attributed and unattributed rows mix, also beside a shared-root legacy row",
    ),
    pytest.param(
        _rows(("claude", CLAUDE_SKILL), ("mcp", "demo-server")),
        {},
        TargetsResult(KNOWN, ("claude",)),
        id="an MCP service row without a runtime is not a deployment target",
    ),
    # --- MCP deployments are recorded per scope, so they rarely name the package
    pytest.param(
        _rows(("mcp:kiro", "a", ["."]), ("mcp:codex", "a", ["."])),
        {"mcp_count": 2},
        TargetsResult(UNAVAILABLE),
        id="an MCP-only package with scope-level MCP rows has unknown targets",
    ),
    pytest.param(
        _rows(("mcp:kiro", "a"), ("mcp:codex", "a")),
        {"mcp_count": 2},
        TargetsResult(KNOWN, ("codex", "kiro")),
        id="an MCP-only package with MCP rows naming it as owner shows those targets",
    ),
    pytest.param(
        _rows(("claude", CLAUDE_SKILL), ("codex", SHARED), ("mcp:claude", "a", ["."])),
        _MCP_SCOPE,
        TargetsResult(PARTIAL, ("claude", "codex"), (NOTE_MCP_SCOPE,)),
        id="primitive targets plus unattributed MCP are partial",
    ),
    pytest.param(
        OWNED_BY_OTHER,
        {"mcp_count": 0, "has_primitives": False},
        TargetsResult(KNOWN, ()),
        id="nothing to deploy and nothing recorded is known empty (a dash)",
    ),
    pytest.param(
        OWNED_BY_OTHER,
        {"has_primitives": True},
        TargetsResult(UNAVAILABLE),
        id="deployable content with no record is unknown, not a dash",
    ),
    pytest.param(
        OWNED_BY_OTHER,
        {"mcp_count": None},
        TargetsResult(UNAVAILABLE),
        id="an unreadable MCP count with no record is unknown",
    ),
    # --- an unreadable MCP count cannot rule MCP deployments out
    pytest.param(
        _rows(("claude", CLAUDE_SKILL), ("codex", SHARED)),
        {"mcp_count": None, "has_primitives": True},
        TargetsResult(PARTIAL, ("claude", "codex"), (NOTE_MCP_UNREADABLE,)),
        id="known targets with an unreadable MCP count carry their own cause",
    ),
    pytest.param(
        _rows(("claude", CLAUDE_SKILL), ("mcp:claude", "a")),
        {"mcp_count": None, "has_primitives": True},
        TargetsResult(KNOWN, ("claude",)),
        id="an unreadable MCP count is no cause when MCP rows name the package",
    ),
    pytest.param(
        CLAUDE_AND_LEGACY,
        {"mcp_count": None, "has_primitives": True},
        TargetsResult(PARTIAL, ("claude",), (NOTE_LEGACY, NOTE_MCP_UNREADABLE)),
        id="legacy and unreadable-MCP causes are listed together in a fixed order",
    ),
    # --- scope: only a recorded scope that contradicts the path is exclusive
    pytest.param(
        _rows(("claude", CLAUDE_SKILL, [SKILLY], "user"), ("copilot", ".copilot/s", [SKILLY])),
        {"scope": "project"},
        TargetsResult(KNOWN, ("copilot",)),
        id="project listing excludes a row recorded for the user scope",
    ),
    pytest.param(
        _rows(("claude", CLAUDE_SKILL, [SKILLY], "user"), ("copilot", ".copilot/s", [SKILLY])),
        {"scope": "user"},
        TargetsResult(KNOWN, ("claude",)),
        id="user listing excludes a row recorded for the project scope",
    ),
    # --- ownership
    pytest.param(
        _rows(("claude", ".claude/shared.md", [SKILLY, OTHER])),
        {"owner": SKILLY},
        TargetsResult(KNOWN, ("claude",)),
        id="shared ownership credits the first co-owner",
    ),
    pytest.param(
        _rows(("claude", ".claude/shared.md", [SKILLY, OTHER])),
        {"owner": OTHER},
        TargetsResult(KNOWN, ("claude",)),
        id="shared ownership credits the second co-owner",
    ),
    pytest.param(
        _rows(("claude", ".claude/shared.md", [SKILLY, OTHER])),
        {"owner": THIRD},
        TargetsResult(KNOWN, ()),
        id="shared ownership does not credit a non-owner",
    ),
    # --- shared roots: credit the target recorded on the row, never the directory
    pytest.param(
        _rows(("codex", SHARED), ("codex", SHARED + "/SKILL.md")),
        {"has_primitives": True},
        TargetsResult(KNOWN, ("codex",)),
        id="a shared-root row is credited only to its recorded target",
    ),
    pytest.param(
        _rows(("codex", SHARED), ("agent-skills", SHARED)),
        {"has_primitives": True},
        TargetsResult(KNOWN, ("agent-skills", "codex")),
        id="each target sharing a directory is credited by its own row",
    ),
]


@pytest.mark.parametrize(("rows", "kwargs", "expected"), TARGET_CASES)
def test_package_targets_reports_what_the_recorded_rows_support(
    rows: Rows, kwargs: dict[str, object], expected: TargetsResult
) -> None:
    options = {"owner": SKILLY, "scope": "project", **kwargs}
    owner = options.pop("owner")
    assert package_targets(_lock(rows), owner, **options) == expected


@pytest.mark.parametrize(
    ("lockfile", "owner", "scope"),
    [
        pytest.param(
            _lock(deployed_files=[".copilot/skills/hello/SKILL.md", ".claude/skills/hello/x"]),
            SKILLY,
            "project",
            id="a legacy lockfile has no recorded deployments in the project scope",
        ),
        pytest.param(
            _lock(deployed_files=[".copilot/skills/hello/SKILL.md", ".claude/skills/hello/x"]),
            SKILLY,
            "user",
            id="a legacy lockfile has no recorded deployments in the user scope",
        ),
        pytest.param(None, SKILLY, "project", id="no lockfile"),
        pytest.param(_lock([]), None, "project", id="no owner key"),
    ],
)
def test_without_recorded_deployments_no_target_is_attributable(
    lockfile: LockFile | None, owner: str | None, scope: str
) -> None:
    assert package_targets(lockfile, owner, scope=scope) == TargetsResult(UNAVAILABLE)


# ---------------------------------------------------------------------------
# package_mcp_count
# ---------------------------------------------------------------------------

COUNT_CASES = [
    pytest.param(lambda r: _manifest(r, [GOOD_SERVER, STDIO_SERVER]), 2, id="two in apm.yml"),
    pytest.param(lambda r: _manifest(r, [GOOD_SERVER]), 1, id="one in apm.yml"),
    pytest.param(lambda r: _manifest(r), 0, id="no mcp block counts zero"),
    pytest.param(lambda r: _manifest(r, []), 0, id="an empty mcp list counts zero"),
    pytest.param(
        lambda r: _manifest(r, [GOOD_SERVER, GOOD_SERVER]),
        1,
        id="identical declarations collapse the way install collapses them",
    ),
    pytest.param(
        lambda r: _agent_plugin(
            r, {"one": {"type": "stdio", "command": "a"}, "two": {"type": "stdio", "command": "b"}}
        ),
        2,
        id="a native Agent Plugin counts through the canonical loader",
    ),
    pytest.param(lambda r: _agent_plugin(r), 0, id="a native Agent Plugin without MCP counts zero"),
    pytest.param(
        lambda r: _agent_plugin(
            r, {"good": {"type": "stdio", "command": "run"}, "bad": {"type": "nonsense"}}
        ),
        1,
        id="one invalid plugin server is not admitted but the count stays readable",
    ),
    pytest.param(
        _claude_plugin, 1, id="a Claude plugin counts from the manifest install generates"
    ),
    pytest.param(_skill_package, 0, id="a skill package declares no servers"),
    pytest.param(_hook_package, 0, id="a hook package declares no servers"),
]


@pytest.mark.parametrize(("build", "expected"), COUNT_CASES)
def test_mcp_count_is_the_number_of_servers_install_would_admit(
    tmp_path: Path, build: Callable[[Path], Path], expected: int
) -> None:
    assert package_mcp_count(build(tmp_path / "pkg")) == McpResult(expected)


_POSIX_PERMISSIONS = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permission bits enforced for the current user",
)

INSPECTION_FAILURES = [
    pytest.param(
        _unreadable_manifest, id="unreadable manifest file (OSError)", marks=_POSIX_PERMISSIONS
    ),
    pytest.param(
        _unreadable_directory, id="unreadable package directory (OSError)", marks=_POSIX_PERMISSIONS
    ),
    pytest.param(
        lambda r: _manifest_text(r, b"name: \xff\xfe\n"), id="manifest not UTF-8 (YAMLError)"
    ),
    pytest.param(lambda r: _manifest_text(r, "name: [x\n"), id="manifest not YAML (YAMLError)"),
    pytest.param(lambda r: _manifest_text(r, "- a\n- b\n"), id="manifest is a list"),
    pytest.param(
        lambda r: _manifest_text(r, "name: p\ndependencies: nope\n"),
        id="dependencies is not a mapping (ValueError)",
    ),
    pytest.param(
        lambda r: _manifest_entry(r, {"transport": "http"}),
        id="mcp entry without a name (ValueError)",
    ),
    pytest.param(
        lambda r: _manifest_entry(r, {**GOOD_SERVER, "transport": "pigeon"}),
        id="mcp unsupported transport (ValueError)",
    ),
    pytest.param(
        lambda r: _manifest_entry(r, {**GOOD_SERVER, "name": 5}),
        id="mcp name of the wrong type (TypeError in the parser)",
    ),
    pytest.param(
        lambda r: _manifest_entry(r, {**GOOD_SERVER, "headers": 3}),
        id="mcp headers of the wrong type (AttributeError in the parser)",
    ),
    pytest.param(
        lambda r: _manifest_entry(r, {**GOOD_SERVER, "url": 5}),
        id="mcp url of the wrong type (AttributeError in the parser)",
    ),
    pytest.param(
        lambda r: _agent_plugin(r, plugin_json="{not json"),
        id="plugin manifest not JSON (AgentPluginError)",
    ),
    pytest.param(
        lambda r: _agent_plugin(r, plugin_json=json.dumps({"$schema": PLUGIN_SCHEMA_ID})),
        id="plugin manifest without a name (AgentPluginError)",
    ),
    pytest.param(
        lambda r: _plugin_mcp_document(r, "{bad"), id="plugin mcp.json not JSON (document disabled)"
    ),
    pytest.param(
        lambda r: _plugin_mcp_document(r, "[]"),
        id="plugin mcp.json not an object (document disabled)",
    ),
    pytest.param(_plugin_mcp_directory, id="plugin mcp.json not a regular file (location invalid)"),
    pytest.param(lambda r: r, id="package directory missing (AgentPluginError)"),
    pytest.param(_empty_dir, id="directory of an unrecognised package type"),
]


@pytest.mark.parametrize("build", INSPECTION_FAILURES)
def test_an_uninspectable_package_has_an_unavailable_mcp_count(
    tmp_path: Path, build: Callable[[Path], Path]
) -> None:
    root = build(tmp_path / "pkg")
    try:
        assert package_mcp_count(root) == McpResult(None)
    finally:
        if root.exists():  # restore permissions so pytest can clean up
            root.chmod(0o755)
            if (root / "apm.yml").exists():
                (root / "apm.yml").chmod(0o644)


_LOAD_YAML = "apm_cli.commands.deps._inventory.load_yaml"
_PARSER = "apm_cli.integration.mcp_config_view.authored_mcp_declarations"
_ROUTE = "apm_cli.bundle.local_bundle.route_agent_plugin_package"
_DETECT = "apm_cli.models.validation.detect_package_type"


@pytest.mark.parametrize(
    ("target", "error", "has_manifest"),
    [
        pytest.param(_LOAD_YAML, RuntimeError("bug"), True, id="load_yaml RuntimeError"),
        pytest.param(_PARSER, RuntimeError("bug"), True, id="parser RuntimeError"),
        pytest.param(_PARSER, KeyError("bug"), True, id="parser KeyError"),
        pytest.param(_ROUTE, TypeError("bug"), False, id="plugin routing TypeError"),
        pytest.param(_ROUTE, AttributeError("bug"), False, id="plugin routing AttributeError"),
        pytest.param(_DETECT, AttributeError("bug"), False, id="type detection AttributeError"),
        pytest.param(_DETECT, RuntimeError("bug"), False, id="type detection RuntimeError"),
    ],
)
def test_programming_errors_propagate_instead_of_reading_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    error: Exception,
    has_manifest: bool,
) -> None:
    package = (
        _manifest(tmp_path / "pkg", [GOOD_SERVER]) if has_manifest else _empty_dir(tmp_path / "pkg")
    )

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise error

    _patch(monkeypatch, target, _boom)
    if target == _DETECT:  # reach the detection call: no plugin is routed
        _patch(monkeypatch, _ROUTE, lambda *_a, **_k: None)
    with pytest.raises(type(error)):
        package_mcp_count(package)


def test_inspecting_a_plugin_is_read_only_and_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _agent_plugin(tmp_path / "plug", {"one": {"type": "stdio", "command": "run-one"}})
    with _offline_and_unchanged(tmp_path, monkeypatch):
        assert package_mcp_count(plugin) == McpResult(1)


def test_the_public_declarations_wrapper_matches_the_install_parser() -> None:
    from apm_cli.integration.mcp_config_view import (
        _authored_mcp_declarations,
        authored_mcp_declarations,
    )

    manifest = {"dependencies": {"mcp": [GOOD_SERVER]}}
    assert authored_mcp_declarations(manifest) == _authored_mcp_declarations(manifest)
    assert list(authored_mcp_declarations(manifest)) == ["a"]
    with pytest.raises(ValueError, match="mapping"):
        authored_mcp_declarations({"dependencies": "nope"})


# ---------------------------------------------------------------------------
# cell text and legend
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "text"),
    [
        (TargetsResult(KNOWN, ("claude", "codex")), "claude, codex"),
        (TargetsResult(KNOWN, ()), "-"),
        (TargetsResult(UNAVAILABLE), "unknown"),
        (TargetsResult(AMBIGUOUS), "ambiguous"),
        (TargetsResult(PARTIAL, ("claude", "codex"), (NOTE_LEGACY,)), "claude, codex*"),
        (TargetsResult(PARTIAL, ("claude", "codex"), (NOTE_MCP_SCOPE,)), "claude, codex*"),
        (None, "unknown"),
    ],
    ids=["known", "nothing", "unknown", "ambiguous", "partial-legacy", "partial-mcp", "no-data"],
)
def test_targets_cell_text_for_each_evidence_level(result: TargetsResult | None, text: str) -> None:
    assert format_targets(result) == text


@pytest.mark.parametrize(
    ("result", "text"),
    [(McpResult(2), "2"), (McpResult(0), "0"), (McpResult(None), "?"), (None, "?")],
    ids=["count", "zero", "unavailable", "no-data"],
)
def test_mcps_cell_text_is_a_count_zero_or_a_question_mark(
    result: McpResult | None, text: str
) -> None:
    assert format_mcps(result) == text


CAUSE_TEXT = {
    NOTE_LEGACY: "legacy records",
    NOTE_MCP_SCOPE: "MCP servers recorded for the whole scope",
    NOTE_MCP_UNREADABLE: "MCP definitions couldn't be read",
}


def _starred(*notes: str) -> TargetsResult:
    return TargetsResult(PARTIAL, ("claude",), notes)


def _legend(causes: tuple[str, ...] = (), *, unattributable: bool = False) -> str:
    parts = []
    if causes:
        parts.append(
            "* some deployments can't be attributed to a target: "
            + ", ".join(CAUSE_TEXT[cause] for cause in causes)
        )
    if unattributable:
        parts.append("unknown/ambiguous: none can be attributed")
    return f"Targets: {'; '.join(parts)}."


_ATTRIBUTED = [TargetsResult(KNOWN, ("claude",)), TargetsResult(KNOWN, ())]
_CAUSE_COMBINATIONS = [
    combo for size in (1, 2, 3) for combo in itertools.combinations(CAUSE_TEXT, size)
]

LEGEND_CASES = [
    pytest.param([], None, id="no cells"),
    pytest.param(_ATTRIBUTED, None, id="every cell attributed or a dash"),
    *[
        pytest.param(
            [_starred(cause) for cause in combo],
            _legend(combo),
            id="causes spread over cells: " + " + ".join(combo),
        )
        for combo in _CAUSE_COMBINATIONS
    ],
    pytest.param(
        [_starred(NOTE_MCP_UNREADABLE, NOTE_LEGACY), _starred(NOTE_LEGACY)],
        _legend((NOTE_LEGACY, NOTE_MCP_UNREADABLE)),
        id="a cell with several causes lists each once, in a fixed order",
    ),
    *[
        pytest.param([cell], _legend(unattributable=True), id=f"{name} cell: its own short legend")
        for name, cell in (
            ("unknown", TargetsResult(UNAVAILABLE)),
            ("ambiguous", TargetsResult(AMBIGUOUS)),
            ("missing-data", None),
        )
    ],
    pytest.param(
        [_starred(NOTE_LEGACY), TargetsResult(UNAVAILABLE)],
        _legend((NOTE_LEGACY,), unattributable=True),
        id="a star cause and an unknown cell share one line",
    ),
]


@pytest.mark.parametrize(("cells", "expected"), LEGEND_CASES)
def test_the_legend_names_only_the_causes_present_in_the_listing(
    cells: list[TargetsResult | None], expected: str | None
) -> None:
    legend = targets_legend(cells)
    assert legend == expected
    assert legend is None or "\n" not in legend


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _plain_rendering() -> Iterator[None]:
    keys = ["rich", "rich.console", "rich.table", "rich.tree", "rich.panel", "rich.text"]
    saved = {k: sys.modules.get(k) for k in keys}
    for k in keys:
        stub = types.ModuleType(k)
        stub.__path__ = []

        def _raise(name: str, _k: str = k) -> None:
            raise ImportError(_k)

        stub.__getattr__ = _raise
        sys.modules[k] = stub
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _installed(root: Path, name: str, *, mcp: list[object] | None = None, skills: int = 0) -> Path:
    pkg = _manifest(root / "apm_modules" / "_local" / name, mcp, name=name)
    for i in range(skills):
        skill = pkg / ".apm" / "skills" / f"s{i}"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\nx\n")
    return pkg


def _write_project(
    root: Path,
    deps: list[dict[str, object]],
    rows: Rows | None = None,
    manifest_deps: list[object] | None = None,
) -> None:
    manifest = {
        "name": "proj",
        "version": "0.1.0",
        "dependencies": {"apm": manifest_deps or [d["local_path"] for d in deps]},
    }
    (root / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    lock: dict[str, object] = {"lockfile_version": "1", "dependencies": deps}
    if rows is not None:
        lock["deployments"] = rows
    (root / "apm.lock.yaml").write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")


def _list(
    root: Path,
    *args: str,
    plain: bool,
    user_dir: Path | None = None,
    width: int = 200,
) -> Result:
    from apm_cli.core.scope import InstallScope

    def _dir(scope: object) -> Path:
        return user_dir if (scope is InstallScope.USER and user_dir) else root

    ctx = _plain_rendering() if plain else contextlib.nullcontext()
    with patch("apm_cli.core.scope.get_apm_dir", side_effect=_dir), ctx:
        return CliRunner().invoke(cli, ["deps", "list", *args], terminal_width=width)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _row_text(output: str, name: str) -> str:
    """The package's row with borders removed, identical for Rich and plain output."""
    line = next(ln for ln in output.splitlines() if name in ln)
    return _flat(line.replace(BOX_VERTICAL, " "))


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


TWO_SERVERS = [GOOD_SERVER, STDIO_SERVER]
CLAUDE_AND_CODEX = _rows(("claude", ".claude/skills/s0"), ("codex", ".agents/skills/s0"))

LISTING_CASES = [
    pytest.param(
        {"skills": 1, "rows": CLAUDE_AND_CODEX},
        "0 claude, codex",
        None,
        id="every record attributed: no legend",
    ),
    pytest.param(
        {
            "skills": 1,
            "rows": CLAUDE_AND_CODEX,
            "args": ("--insecure",),
            "lock": {"is_insecure": True},
        },
        "0 claude, codex",
        None,
        id="the --insecure view keeps Origin and gains both columns",
    ),
    pytest.param(
        {"skills": 1, "rows": _rows(("claude", ".claude/skills/s0"), ("legacy", SHARED))},
        "0 claude*",
        _legend((NOTE_LEGACY,)),
        id="a legacy record marks the cell and the legend says so",
    ),
    pytest.param(
        {
            "skills": 1,
            "mcp": [GOOD_SERVER],
            "rows": _rows(("claude", ".claude/skills/s0"), ("mcp:claude", "a", ["."])),
        },
        "1 claude*",
        _legend((NOTE_MCP_SCOPE,)),
        id="MCP servers recorded for the whole scope mark the cell",
    ),
    pytest.param(
        {"skills": 1, "rows": _rows(("claude", ".claude/skills/s0")), "count": McpResult(None)},
        "? claude*",
        _legend((NOTE_MCP_UNREADABLE,)),
        id="an uninspectable MCP count marks otherwise known targets",
    ),
    pytest.param(
        {"skills": 1, "rows": None, "lock": {"deployed_files": [".claude/skills/s0/SKILL.md"]}},
        "0 unknown",
        _legend(unattributable=True),
        id="a legacy lockfile shows unknown targets",
    ),
    pytest.param(
        {"skills": 1, "rows": [], "count": McpResult(None)},
        "? unknown",
        _legend(unattributable=True),
        id="uninspectable content with no record shows ? and unknown",
    ),
    pytest.param(
        {"mcp": TWO_SERVERS, "rows": []},
        "2 unknown",
        _legend(unattributable=True),
        id="an MCP-only package with no rows is listed with its count, not a dash",
    ),
    pytest.param(
        {"mcp": TWO_SERVERS, "rows": _rows(("mcp:kiro", "a", ["."]), ("mcp:codex", "a", ["."]))},
        "2 unknown",
        _legend(unattributable=True),
        id="an MCP-only package with scope-level MCP rows shows unknown, not a dash",
    ),
    pytest.param(
        {"mcp": [GOOD_SERVER], "rows": _rows(("claude", ".claude/a"), ("codex", ".codex/a"))},
        "1 claude, codex*",
        _legend((NOTE_MCP_SCOPE,)),
        id="the MCP count is not multiplied by the package's target copies",
    ),
]


@pytest.mark.parametrize("plain", [False, True], ids=["rich", "plain"])
@pytest.mark.parametrize(("setup", "tail", "legend"), LISTING_CASES)
def test_the_package_row_and_legend_reflect_the_recorded_evidence(
    cwd: Path, plain: bool, setup: dict[str, object], tail: str, legend: str | None
) -> None:
    _installed(cwd, "skilly", mcp=setup.get("mcp"), skills=setup.get("skills", 0))
    _write_project(cwd, [_dep(SKILLY, **setup.get("lock", {}))], setup["rows"])
    count = setup.get("count")
    with (
        patch("apm_cli.commands.deps.cli.package_mcp_count", return_value=count)
        if count
        else (contextlib.nullcontext())
    ):
        output = _list(cwd, *setup.get("args", ()), plain=plain).output

    assert all(header in output for header in ("Skills", "Hooks", "MCPs", "Targets"))
    assert _row_text(output, "_local/skilly").endswith(f" {tail}")
    if "args" in setup:
        assert "Origin" in output and "direct" in output
    flat = _flat(output)
    if legend is None:
        assert "Targets: " not in flat
    else:
        assert legend in flat and flat.count("Targets: ") == 1


def test_a_manifestless_skill_package_in_the_listing_declares_no_mcp_servers(cwd: Path) -> None:
    _skill_package(cwd / "apm_modules" / "acme" / "bare")
    manifest = {"name": "p", "version": "1", "dependencies": {"apm": ["acme/bare"]}}
    (cwd / "apm.yml").write_text(yaml.safe_dump(manifest))
    assert " 0 " in f"{_row_text(_list(cwd, plain=True).output, 'acme/bare')} "


def test_all_scope_shows_differing_project_and_user_records(cwd: Path) -> None:
    user_dir = cwd / "home-apm"
    user_dir.mkdir()
    for base, target, scope in ((cwd, "claude", "project"), (user_dir, "codex", "user")):
        _installed(base, "skilly", skills=1)
        _write_project(base, [_dep(SKILLY)], _rows((target, ".x/s", [SKILLY], scope)))
    out = _list(cwd, "--all", plain=True, user_dir=user_dir).output
    project_part, global_part = out.split("APM Dependencies (Global)")
    assert "claude" in project_part and "codex" not in project_part
    assert "codex" in global_part and "claude" not in global_part


def test_an_aliased_package_reads_its_own_records(cwd: Path) -> None:
    _manifest(cwd / "apm_modules" / "my-alias", name="aliased")
    dep = {"repo_url": "acme/aliased", "host": "github.com", "alias": "my-alias"}
    dep["resolved_commit"] = "a" * 40
    locked = LockFile.from_yaml(yaml.safe_dump({"lockfile_version": "1", "dependencies": [dep]}))
    owner = next(iter(locked.dependencies))
    declared = [{"git": "https://github.com/acme/aliased.git", "alias": "my-alias"}]
    _write_project(
        cwd, [dep], _rows(("claude", ".claude/skills/s", [owner])), manifest_deps=declared
    )
    out = _list(cwd, plain=True).output
    row = next(ln for ln in out.splitlines() if "aliased" in ln or "my-alias" in ln)
    assert "claude" in row


def test_listing_is_read_only_and_makes_no_network_calls(
    cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(cwd, "skilly", mcp=[GOOD_SERVER])
    _write_project(cwd, [_dep(SKILLY)], _rows(("claude", ".claude/a")))
    with _offline_and_unchanged(cwd, monkeypatch):
        result = _list(cwd, plain=True)
    assert result.exit_code == 0, result.output
    assert _row_text(result.output, "_local/skilly").endswith(" 1 claude*")


def test_a_real_local_path_install_records_the_targets_the_listing_reads(
    cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APM_NO_TELEMETRY", "1")
    pkg = cwd / "pkgs" / "skilly"
    skill = pkg / ".apm" / "skills" / "hello"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: hello\ndescription: say hello\n---\nHi\n")
    (pkg / "apm.yml").write_text("name: skilly\nversion: 1.0.0\ndescription: one skill\n")
    (cwd / ".claude").mkdir()
    (cwd / ".codex").mkdir()
    (cwd / "apm.yml").write_text(
        "name: proj\nversion: 0.1.0\ndescription: d\ndependencies:\n  apm:\n    - ./pkgs/skilly\n"
    )
    install = CliRunner().invoke(cli, ["install", "--target", "claude,codex"])
    assert install.exit_code == 0, install.output

    lock = LockFile.read(cwd / "apm.lock.yaml")
    owner = next(k for k, d in lock.dependencies.items() if d.local_path == "./pkgs/skilly")
    assert package_targets(lock, owner, scope="project").targets == ("claude", "codex")
    # Listing discovery is untouched: the installed package is found where it always was.
    assert _row_text(_list(cwd, plain=True).output, "_local/skilly").endswith(" claude, codex")


@pytest.mark.parametrize("plain", [False, True], ids=["rich", "plain"])
def test_a_narrow_terminal_keeps_rows_aligned_and_the_legend_intact(
    cwd: Path, monkeypatch: pytest.MonkeyPatch, plain: bool
) -> None:
    # The width policy is untouched: Rich keeps its 120-column floor and the plain
    # table never wraps, so an 80-column terminal must still render cleanly.
    monkeypatch.setenv("COLUMNS", "80")
    packages = {"both": (1, [GOOD_SERVER]), "skilly": (1, None), "mcponly": (0, [GOOD_SERVER])}
    for name, (skills, mcp) in packages.items():
        _installed(cwd, name, skills=skills, mcp=mcp)
    rows = _rows(
        ("claude", ".claude/skills/both-s0", ["./pkgs/both"]),
        ("claude", ".claude/skills/skilly-s0", ["./pkgs/skilly"]),
        ("codex", ".agents/skills/s0", ["./pkgs/skilly"]),
        ("mcp:claude", "a", ["."]),
    )
    _write_project(cwd, [_dep(f"./pkgs/{name}") for name in packages], rows)
    output = _list(cwd, plain=plain, width=80).output
    lines = output.splitlines()
    assert _legend((NOTE_MCP_SCOPE,), unattributable=True) in _flat(output)

    if plain:
        header = next(ln for ln in lines if ln.startswith("Package"))
        mcps_end, targets_at = header.index("MCPs") + 4, header.index("Targets")
        expected = {
            "_local/both": ("1", "claude*"),
            "_local/skilly": ("0", "claude, codex"),
            "_local/mcponly": ("1", "unknown"),
        }
        listed = {ln.split()[0]: ln for ln in lines if ln.startswith("_local/")}
        assert set(listed) == set(expected)
        for name, (mcps, targets) in expected.items():
            row = listed[name]
            assert row[mcps_end - 4 : mcps_end].strip() == mcps
            assert row[targets_at - 1] == " " and row[targets_at:] == targets
        rule = lines[lines.index(header) + 1]
        assert set(rule) == {"-"} and len(rule) == len(header)
    else:
        table = [ln for ln in lines if ln and ln[0] in RICH_TABLE_LINE_STARTS]
        assert len({len(ln) for ln in table}) == 1, "table rows differ in width"
        assert len(table) >= 5 and not any(ELLIPSIS in ln for ln in table)
        # 10 columns have 11 vertical bars
        assert all(ln.count(BOX_VERTICAL) == 11 for ln in table if ln[0] == BOX_VERTICAL)
