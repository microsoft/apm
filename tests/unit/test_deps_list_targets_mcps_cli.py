"""Tests for the Targets and MCPs columns of ``apm deps list`` (#2976)."""

from __future__ import annotations

import contextlib
import importlib
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner, Result

from apm_cli.cli import cli
from apm_cli.commands.deps._inventory import (
    NOTE_LEGACY,
    NOTE_MCP_SCOPE,
    NOTE_MCP_UNREADABLE,
    McpResult,
    package_targets,
)
from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import DeploymentLedger
from apm_cli.deps.lockfile import LockFile
from tests.unit.test_deps_list_targets_mcps import (
    BOX_VERTICAL,
    ELLIPSIS,
    GOOD_SERVER,
    RICH_TABLE_LINE_STARTS,
    SHARED,
    SKILLY,
    STDIO_SERVER,
    Rows,
    _dep,
    _legend,
    _manifest,
    _offline_and_unchanged,
    _patch,
    _records_for,
    _rows,
    _skill_package,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-deployment-provenance-state"),
]


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
    ctx = _plain_rendering() if plain else contextlib.nullcontext()
    with pytest.MonkeyPatch.context() as env, ctx:
        env.chdir(root)
        env.setenv("APM_HOME", str(user_dir if user_dir is not None else root))
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
        {"rows": None, "lock": {"deployed_files": [".claude/x/y.md"]}},
        "0 unknown",
        _legend(unattributable=True),
        id="a legacy lockfile reads as no evidence even for a package with nothing to deploy",
    ),
    pytest.param(
        {"skills": 1, "rows": [], "count": McpResult(None)},
        "? unknown",
        _legend(unattributable=True),
        id="uninspectable content with no record shows ? and unknown",
    ),
    pytest.param(
        {"skills": 1, "mcp": [{**GOOD_SERVER, "transport": "pigeon"}], "rows": []},
        "? unknown",
        _legend(unattributable=True),
        id="a malformed MCP declaration keeps the package listed with ? MCPs",
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


@pytest.mark.parametrize(
    ("manifest", "version"),
    [
        ("name: skilly\nversion: ''\n", "unknown"),
        ("name: ''\nversion: 1.0.0\n", "1.0.0"),
        ("name: skilly\nversion: 1.0\n", "unknown"),
    ],
    ids=["empty version", "empty name", "version that is not a string"],
)
def test_a_manifest_rejected_for_a_non_mcp_reason_is_listed_with_a_normal_mcp_count(
    cwd: Path, manifest: str, version: str
) -> None:
    # The strict parser rejects these, the tolerant display read does not: the package
    # stays listed and MCPs is "?" only when the MCP declarations themselves are unreadable.
    pkg = _installed(cwd, "skilly", skills=1)
    (pkg / "apm.yml").write_text(manifest, encoding="utf-8")
    _write_project(cwd, [_dep(SKILLY)], _rows(("claude", ".claude/skills/s0")))
    row = _row_text(_list(cwd, plain=True).output, "_local/skilly")
    assert row.split()[1] == version
    assert row.endswith(" 0 claude")


def test_a_package_whose_yaml_cannot_be_parsed_keeps_the_read_failure_warning(cwd: Path) -> None:
    # Unchanged contract: only a manifest the strict parser rejects but whose YAML parses
    # is still listed; unparseable YAML is reported without a row.
    pkg = _installed(cwd, "skilly", skills=1)
    (pkg / "apm.yml").write_text("name: [unclosed\n", encoding="utf-8")
    _write_project(cwd, [_dep(SKILLY)], _rows(("claude", ".claude/skills/s0")))
    out = _list(cwd, plain=True).output
    assert "Failed to inspect package _local/skilly" in out
    assert not any(ln.startswith("_local/skilly") for ln in out.splitlines())


def test_a_package_missing_from_the_lockfile_has_unknown_targets(cwd: Path) -> None:
    _installed(cwd, "skilly", skills=1)
    _installed(cwd, "stray", skills=1)
    _installed(cwd, "empty")  # nothing to deploy: unknown, not a dash that reads as "not deployed"
    _write_project(cwd, [_dep(SKILLY)], _rows(("claude", ".claude/skills/s0")))
    out = _list(cwd, plain=True).output
    assert _row_text(out, "_local/skilly").endswith(" 0 claude")
    assert _row_text(out, "_local/stray").endswith(" 0 unknown")
    assert _row_text(out, "_local/empty").endswith(" 0 unknown")


def test_a_manifestless_skill_package_in_the_listing_declares_no_mcp_servers(cwd: Path) -> None:
    _skill_package(cwd / "apm_modules" / "acme" / "bare")
    manifest = {"name": "p", "version": "1", "dependencies": {"apm": ["acme/bare"]}}
    (cwd / "apm.yml").write_text(yaml.safe_dump(manifest))
    assert _row_text(_list(cwd, plain=True).output, "acme/bare").endswith(" 0 unknown")


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


def _lock_owners(deps: list[dict[str, object]]) -> list[str]:
    locked = LockFile.from_yaml(yaml.safe_dump({"lockfile_version": "1", "dependencies": deps}))
    return list(locked.dependencies)


def test_aliased_packages_with_one_owner_and_repo_on_two_hosts_keep_their_own_targets(
    cwd: Path,
) -> None:
    deps = [
        {
            "repo_url": "acme/tool",
            "host": "github.com",
            "alias": "gh-tool",
            "resolved_commit": "a" * 40,
        },
        {
            "repo_url": "acme/tool",
            "host": "gitlab.com",
            "alias": "gl-tool",
            "resolved_commit": "b" * 40,
        },
    ]
    github_owner, gitlab_owner = _lock_owners(deps)
    for alias in ("gh-tool", "gl-tool"):
        _manifest(cwd / "apm_modules" / alias, name="tool")
    declared = [
        {"git": "https://github.com/acme/tool.git", "alias": "gh-tool"},
        {"git": "https://gitlab.com/acme/tool.git", "alias": "gl-tool"},
    ]
    rows = _rows(("claude", ".claude/a", [github_owner]), ("codex", ".agents/b", [gitlab_owner]))
    _write_project(cwd, deps, rows, manifest_deps=declared)
    out = _list(cwd, plain=True).output
    tails = sorted(_flat(ln).split()[-1] for ln in out.splitlines() if "tool" in ln)
    assert tails == ["claude", "codex"]


def test_a_package_materialized_with_different_letter_case_still_finds_its_targets(
    cwd: Path,
) -> None:
    dep = {"repo_url": "acme/tool", "host": "github.com", "resolved_commit": "a" * 40}
    (owner,) = _lock_owners([dep])
    _manifest(cwd / "apm_modules" / "Acme" / "Tool", name="tool")
    _write_project(cwd, [dep], _rows(("claude", ".claude/a", [owner])), manifest_deps=["acme/tool"])
    out = _list(cwd, plain=True).output
    row = next(ln for ln in out.splitlines() if "tool" in ln.lower())
    assert _flat(row).endswith(" claude")


class _ScanCountingRecords(dict):
    """Records mapping that counts how many times the ledger is walked."""

    scans = 0

    def values(self):  # type: ignore[no-untyped-def]
        type(self).scans += 1
        return super().values()


def test_the_ledger_is_scanned_once_per_listing_not_once_per_package(
    cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = [f"p{i}" for i in range(6)]
    for name in names:
        _installed(cwd, name, skills=1)
    rows = _rows(*[("claude", f".claude/skills/{n}", [f"./pkgs/{n}"]) for n in names])
    _write_project(cwd, [_dep(f"./pkgs/{n}") for n in names], rows)

    class _CountingCodec(DeploymentLedgerCodec):
        @staticmethod
        def recorded_owner_index(lockfile: LockFile, *, scope: str):
            ledger = lockfile.deployment_ledger
            assert ledger is not None
            lockfile.deployment_ledger = DeploymentLedger(
                records=_ScanCountingRecords(ledger.records)
            )
            return DeploymentLedgerCodec.recorded_owner_index(lockfile, scope=scope)

    _ScanCountingRecords.scans = 0
    monkeypatch.setattr(
        importlib.import_module("apm_cli.commands.deps.cli"),
        "DeploymentLedgerCodec",
        _CountingCodec,
    )
    out = _list(cwd, plain=True).output
    assert all(_row_text(out, f"_local/{n}").endswith(" claude") for n in names)
    assert _ScanCountingRecords.scans == 1


def test_a_lockfile_entry_whose_path_cannot_be_resolved_does_not_affect_other_rows(
    cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.models.dependency.materialization import installed_package_path
    from apm_cli.utils.path_security import PathTraversalError

    names = ["bad", "good"]
    for name in names:
        _installed(cwd, name, skills=1)
    rows = _rows(*[("claude", f".claude/skills/{n}", [f"./pkgs/{n}"]) for n in names])
    _write_project(cwd, [_dep(f"./pkgs/{n}") for n in names], rows)

    def _resolve(dependency: object, apm_modules_dir: Path) -> Path:
        if "bad" in str(getattr(dependency, "local_path", "")):
            raise PathTraversalError("unresolvable")
        return installed_package_path(dependency, apm_modules_dir)

    _patch(monkeypatch, "apm_cli.commands.deps.cli.installed_package_path", _resolve)
    result = _list(cwd, plain=True)
    assert result.exit_code == 0, result.output
    assert _row_text(result.output, "_local/good").endswith(" claude")
    assert _row_text(result.output, "_local/bad").endswith(" unknown")


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
    assert package_targets(_records_for(lock, owner)).targets == ("claude", "codex")
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
        # The ASCII header and three package rows each have 10 cells.
        cells = [ln for ln in table if ln.startswith("| ")]
        assert len(cells) == 1 + len(packages)
        assert all(ln.count(BOX_VERTICAL) == 11 for ln in cells)
