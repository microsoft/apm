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
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from apm_cli.agent_plugins import MCP_SCHEMA_ID, PLUGIN_SCHEMA_ID
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
from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import DeploymentRecord
from apm_cli.deps.lockfile import LockFile
from apm_cli.models.validation import PackageType, detect_package_type

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-deployment-provenance-state"),
]

# APM's Rich tables use the ASCII box on every terminal.
BOX_VERTICAL = "|"
RICH_TABLE_LINE_STARTS = "+|"
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


def _records_for(
    lockfile: LockFile, owner: str = SKILLY, scope: str = "project"
) -> tuple[DeploymentRecord, ...] | None:
    """The owner's rows as the listing resolves them: ``None`` without recorded evidence."""
    index = DeploymentLedgerCodec.recorded_owner_index(lockfile, scope=scope)
    return None if index is None else index.get(owner, ())


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


def _plugin_with_metadata_only_manifest(root: Path) -> Path:
    """A native Agent Plugin whose apm.yml carries only a name and version."""
    _agent_plugin(root, {"one": {"type": "stdio", "command": "run-one"}})
    (root / "apm.yml").write_text("name: native.plugin\nversion: 1.0.0\n", encoding="utf-8")
    return root


def _plugin_with_eligible_manifest(root: Path) -> Path:
    """A plugin.json beside an apm.yml that declares dependencies (and one server)."""
    _agent_plugin(
        root, {"a": {"type": "stdio", "command": "a"}, "b": {"type": "stdio", "command": "b"}}
    )
    return _manifest(root, [GOOD_SERVER])


def _eligible_manifest_with_broken_plugin_json(root: Path) -> Path:
    """apm.yml that declares dependencies beside a plugin.json that is not JSON."""
    _manifest(root, [GOOD_SERVER])
    (root / "plugin.json").write_text("{not json", encoding="utf-8")
    return root


def _metadata_manifest_with_broken_plugin_json(root: Path) -> Path:
    """A metadata-only apm.yml beside a plugin.json that is not JSON."""
    root.mkdir(parents=True)
    (root / "apm.yml").write_text("name: native.plugin\nversion: 1.0.0\n", encoding="utf-8")
    (root / "plugin.json").write_text("{not json", encoding="utf-8")
    return root


def _plugin_with_metadata_manifest_and_broken_mcp_document(root: Path) -> Path:
    """A valid plugin and matching metadata-only apm.yml; the MCP document is not JSON."""
    _plugin_mcp_document(root, "{bad")
    (root / "apm.yml").write_text("name: native.plugin\nversion: 1.0.0\n", encoding="utf-8")
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
    records = _records_for(_lock(rows), options.pop("owner"), options.pop("scope"))
    assert package_targets(records, **options) == expected


@pytest.mark.parametrize("scope", ["project", "user"])
def test_a_legacy_lockfile_has_no_recorded_deployments_in_either_scope(scope: str) -> None:
    legacy = _lock(deployed_files=[".copilot/skills/hello/SKILL.md", ".claude/skills/hello/x"])
    records = _records_for(legacy, scope=scope)
    assert records is None
    assert package_targets(records) == TargetsResult(UNAVAILABLE)


def test_a_package_with_nothing_to_read_has_no_attributable_target() -> None:
    # No lockfile, or a package that is not in it: the listing passes None.
    assert package_targets(None) == TargetsResult(UNAVAILABLE)


# ---------------------------------------------------------------------------
# package_mcp_count
# ---------------------------------------------------------------------------

COUNT_CASES = [
    pytest.param(lambda r: _manifest(r, [GOOD_SERVER, STDIO_SERVER]), 2, id="two in apm.yml"),
    pytest.param(lambda r: _manifest(r, [GOOD_SERVER]), 1, id="one in apm.yml"),
    pytest.param(lambda r: _manifest(r), 0, id="no mcp block counts zero"),
    pytest.param(lambda r: _manifest(r, []), 0, id="an empty mcp list counts zero"),
    pytest.param(
        lambda r: _manifest(r, [GOOD_SERVER, GOOD_SERVER.copy()]),
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
    pytest.param(
        _plugin_with_metadata_only_manifest,
        1,
        id="a metadata-only apm.yml does not hide the servers of the plugin install selects",
    ),
    pytest.param(
        _plugin_with_eligible_manifest,
        1,
        id="an eligible apm.yml wins over plugin.json, as install selects it",
    ),
    pytest.param(
        _eligible_manifest_with_broken_plugin_json,
        1,
        id="an eligible apm.yml still counts when plugin.json is broken, as install ignores it",
    ),
    pytest.param(_skill_package, 0, id="a skill package declares no servers"),
    pytest.param(_hook_package, 0, id="a hook package declares no servers"),
]


@pytest.mark.parametrize(("build", "expected"), COUNT_CASES)
def test_mcp_count_is_the_number_of_servers_install_would_admit(
    tmp_path: Path, build: Callable[[Path], Path], expected: int
) -> None:
    assert package_mcp_count(build(tmp_path / "pkg")) == McpResult(expected)


@pytest.mark.parametrize(
    ("build", "selected"),
    [
        (_plugin_with_metadata_only_manifest, PackageType.AGENT_PLUGIN),
        (_plugin_with_eligible_manifest, PackageType.APM_PACKAGE),
        (_eligible_manifest_with_broken_plugin_json, PackageType.APM_PACKAGE),
        (_metadata_manifest_with_broken_plugin_json, PackageType.INVALID),
        (_plugin_with_metadata_manifest_and_broken_mcp_document, PackageType.AGENT_PLUGIN),
    ],
    ids=[
        "metadata-only apm.yml",
        "eligible apm.yml",
        "eligible apm.yml, broken plugin.json",
        "metadata-only apm.yml, broken plugin.json",
        "plugin, metadata-only apm.yml, broken mcp.json",
    ],
)
def test_the_count_follows_the_package_format_install_selects(
    tmp_path: Path, build: Callable[[Path], Path], selected: PackageType
) -> None:
    assert detect_package_type(build(tmp_path / "pkg"))[0] is selected


_POSIX_PERMISSIONS = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permission bits enforced for the current user",
)

INSPECTION_FAILURES = [
    pytest.param(
        lambda r: _manifest_text(r, "dependencies: {mcp: [first]}\ndependencies: {}\n"),
        id="duplicate dependencies",
    ),
    pytest.param(
        lambda r: _manifest_text(r, "servers: &servers [first]\ndependencies: {mcp: *servers}\n"),
        id="YAML anchor and alias",
    ),
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
    pytest.param(
        _metadata_manifest_with_broken_plugin_json,
        id="metadata-only apm.yml with a broken plugin.json (install rejects the package)",
    ),
    pytest.param(
        _plugin_with_metadata_manifest_and_broken_mcp_document,
        id="plugin with a metadata-only apm.yml and a broken mcp.json (install admits the plugin)",
    ),
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


_LOAD_MANIFEST = "apm_cli.commands.deps._inventory.load_manifest"
_PARSER = "apm_cli.integration.mcp_config_view.authored_mcp_declarations"
_ROUTE = "apm_cli.bundle.local_bundle.route_agent_plugin_package"
_DETECT = "apm_cli.models.validation.detect_package_type"


@pytest.mark.parametrize(
    ("target", "error", "has_manifest"),
    [
        pytest.param(_LOAD_MANIFEST, RuntimeError("bug"), True, id="load_manifest RuntimeError"),
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
