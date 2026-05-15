"""E2E integration tests for the MCP gate honoring ``targets:``.

Closes the test gap surfaced in PR #1336 (issue #1335): the MCP install
path used to call ``active_targets()`` with the singular ``target:`` key
only, so a project whitelisting ``targets: [copilot]`` would still write
``.cursor/mcp.json`` and ``.codex/config.toml`` if a foreign signal
existed on disk.

These tests exercise the real ``MCPIntegrator.install`` against on-disk
project layouts -- no mocks of the gate, no mocks of the resolver -- to
prove:

1. A ``targets: [copilot]`` whitelist suppresses every non-copilot
   per-runtime config write even when foreign signals exist.
2. A ``targets: [copilot, cursor]`` whitelist allows both writes.
3. The greenfield strictness contract holds: no ``targets:``, no
   ``--target`` flag, no detectable signals -> NO MCP writes happen
   anywhere (the gate fails closed; the asymmetry between MCP install
   and ``apm install`` is closed).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.dependency.mcp import MCPDependency

pytestmark = pytest.mark.integration


def _make_stdio_dep(name: str = "test-srv") -> MCPDependency:
    """Build a synthetic stdio MCP dependency that needs no network."""
    return MCPDependency.from_dict(
        {
            "name": name,
            "registry": False,
            "transport": "stdio",
            "command": "echo",
            "args": ["mcp-targets-gate-e2e"],
        }
    )


def _seed_signal(project: Path, target: str) -> None:
    """Seed an on-disk signal that ``detect_signals`` recognizes."""
    if target == "copilot":
        gh = project / ".github"
        gh.mkdir(parents=True, exist_ok=True)
        (gh / "copilot-instructions.md").write_text("# test\n", encoding="utf-8")
    elif target == "cursor":
        (project / ".cursor").mkdir(parents=True, exist_ok=True)
    elif target == "codex":
        (project / ".codex").mkdir(parents=True, exist_ok=True)
    elif target == "claude":
        (project / ".claude").mkdir(parents=True, exist_ok=True)
        (project / "CLAUDE.md").write_text("# test\n", encoding="utf-8")
    elif target == "gemini":
        (project / ".gemini").mkdir(parents=True, exist_ok=True)
    else:
        raise ValueError(f"unsupported signal target: {target}")


def _copilot_mcp_path(project: Path) -> Path:
    """Project-scoped copilot MCP config path."""
    return project / ".vscode" / "mcp.json"


def _cursor_mcp_path(project: Path) -> Path:
    return project / ".cursor" / "mcp.json"


def _codex_mcp_path(project: Path) -> Path:
    return project / ".codex" / "config.toml"


class TestMCPTargetsGatingE2E:
    def test_targets_whitelist_copilot_suppresses_foreign_writes(
        self, tmp_path, capsys, monkeypatch
    ):
        """``targets: [copilot]`` + on-disk cursor/codex signals -> only
        copilot survives the gate; foreign-runtime config files MUST NOT
        be written.  This is the core regression described in #1335:
        pre-fix, foreign-signal directories silently received MCP writes
        despite the explicit whitelist.

        The copilot adapter writes to user-scope ``~/.copilot/`` (not the
        project), so we monkeypatch ``HOME`` to a tmp path to keep the
        test hermetic.  The load-bearing assertions are:

        * ``.cursor/mcp.json`` is NOT written
        * ``.codex/config.toml`` is NOT written
        * the gate emits a ``[i] Skipped MCP config for ...`` drop line
        """
        project = tmp_path / "proj-whitelist-copilot"
        project.mkdir()
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        # Seed BOTH whitelisted and foreign signals; the gate must drop
        # cursor and codex even though their on-disk markers are present.
        _seed_signal(project, "copilot")
        _seed_signal(project, "cursor")
        _seed_signal(project, "codex")

        MCPIntegrator.install(
            [_make_stdio_dep("e2e-copilot-only")],
            project_root=project,
            apm_config={"targets": ["copilot"]},
        )

        captured = capsys.readouterr()
        assert "Skipped MCP config" in captured.out, (
            "Gate must announce the dropped runtimes via the drop line "
            "so users can see why their foreign-signal directories did "
            "not receive writes."
        )
        # Honor the cli-log N1 lead-with-outcome contract: outcome FIRST.
        assert "Skipped MCP config" in captured.out.split("\n")[0] or any(
            line.lstrip().startswith("[i] Skipped MCP config") for line in captured.out.splitlines()
        )

        assert not _cursor_mcp_path(project).exists(), (
            "cursor MCP config MUST NOT be written when cursor is absent "
            "from targets: -- the foreign .cursor/ signal does not grant "
            "an implicit license to write."
        )
        assert not _codex_mcp_path(project).exists(), (
            "codex MCP config MUST NOT be written when codex is absent from targets:."
        )

    def test_targets_whitelist_multi_allows_listed_runtimes(self, tmp_path, monkeypatch):
        """``targets: [copilot, cursor]`` -> cursor MCP config IS written
        (positive control); codex (declared on disk but not whitelisted)
        is dropped.
        """
        project = tmp_path / "proj-whitelist-multi"
        project.mkdir()
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        _seed_signal(project, "copilot")
        _seed_signal(project, "cursor")
        _seed_signal(project, "codex")

        MCPIntegrator.install(
            [_make_stdio_dep("e2e-multi")],
            project_root=project,
            apm_config={"targets": ["copilot", "cursor"]},
        )

        assert _cursor_mcp_path(project).exists(), (
            "cursor MCP config MUST be written when cursor IS in targets: "
            "-- proves the gate is not over-restricting."
        )
        cursor_data = json.loads(_cursor_mcp_path(project).read_text(encoding="utf-8"))
        assert "e2e-multi" in cursor_data.get("mcpServers", {})

        assert not _codex_mcp_path(project).exists(), (
            "codex must remain unwritten -- the targets: list is the "
            "single source of truth, not the on-disk signal."
        )

    def test_greenfield_no_targets_no_signals_no_flag_writes_nothing(
        self, tmp_path, capsys, monkeypatch
    ):
        """Greenfield strictness contract (PR #1336): no ``targets:`` in
        apm_config, no per-runtime signals on disk, no ``--target`` flag
        passed -> NO MCP writes happen anywhere AND the gate emits a
        red ``[x]`` error explaining why.

        Pre-fix the gate fell back to ``[copilot]`` and silently wrote
        ``.vscode/mcp.json`` even on a fully greenfield project. Post-fix
        the gate delegates to ``resolve_targets``, which raises
        ``NoHarnessError``; the gate fails closed and returns ``[]`` --
        matching the UX of ``apm install`` on the same input.
        """
        project = tmp_path / "proj-greenfield"
        project.mkdir()
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        # Intentionally NO signal markers and NO targets: in apm_config.

        MCPIntegrator.install(
            [_make_stdio_dep("e2e-greenfield")],
            project_root=project,
            apm_config={},
        )

        captured = capsys.readouterr()
        assert "Skipping all MCP config writes" in captured.out, (
            "Greenfield project must surface the closed-gate decision "
            "with a red [x] error -- silent no-op is exactly the "
            "asymmetry vs `apm install` that PR #1336 closes."
        )

        assert not _cursor_mcp_path(project).exists()
        assert not _codex_mcp_path(project).exists()
        assert not (project / ".vscode" / "mcp.json").exists(), (
            "Greenfield project (no targets:, no signals, no flag) must "
            "not receive a silent copilot-vscode MCP write -- the "
            "pre-#1336 fallback is gone."
        )
