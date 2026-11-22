"""End-to-end integration tests for the MCP lifecycle across install/update/uninstall.

Exercises the full chain: transitive MCP collection, deduplication,
stale-server removal, and lockfile MCP bookkeeping — using synthetic
package names only (no private/project-specific identifiers).
"""

import json
import os
import tempfile  # noqa: F401
from pathlib import Path

import pytest  # noqa: F401
import yaml

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.integration.mcp_integrator import MCPIntegrator

# ---------------------------------------------------------------------------
# Helpers — mirror the per-file convention used across the test suite.
# ---------------------------------------------------------------------------


def _write_apm_yml(path: Path, deps: list = None, mcp: list = None, name: str = "test-project"):  # noqa: RUF013
    """Write a minimal apm.yml at *path* with optional APM and MCP deps."""
    data = {"name": name, "version": "1.0.0", "dependencies": {}}
    if deps:
        data["dependencies"]["apm"] = deps
    if mcp:
        data["dependencies"]["mcp"] = mcp
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _make_pkg_dir(
    apm_modules: Path,
    repo_url: str,
    mcp: list = None,  # noqa: RUF013
    apm_deps: list = None,  # noqa: RUF013
    name: str = None,  # noqa: RUF013
    virtual_path: str = None,  # noqa: RUF013
):
    """Create a package directory under apm_modules with an apm.yml."""
    base = apm_modules / repo_url
    if virtual_path:
        base = base / virtual_path
    base.mkdir(parents=True, exist_ok=True)
    pkg_name = name or repo_url.split("/")[-1]
    data = {"name": pkg_name, "version": "1.0.0", "dependencies": {}}
    if mcp:
        data["dependencies"]["mcp"] = mcp
    if apm_deps:
        data["dependencies"]["apm"] = apm_deps
    (base / "apm.yml").write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _write_lockfile(path: Path, locked_deps: list, mcp_servers: list = None):  # noqa: RUF013
    """Write a lockfile from LockedDependency list and optional MCP server names."""
    lf = LockFile()
    for dep in locked_deps:
        lf.add_dependency(dep)
    if mcp_servers:
        lf.mcp_servers = mcp_servers
    lf.write(path)


def _write_mcp_json(path: Path, servers: dict):
    """Write a .vscode/mcp.json file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"servers": servers}, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Scenario 1 — Selective install with transitive MCP deps
# ---------------------------------------------------------------------------
class TestSelectiveInstallTransitiveMCP:
    """When `apm install acme/squad-alpha` is requested, the lockfile-scoped
    MCP collector should find MCP servers declared by transitive deps
    of squad-alpha even though squad-alpha itself has no MCP section."""

    def setup_method(self):
        self._orig_cwd = os.getcwd()

    def teardown_method(self):
        os.chdir(self._orig_cwd)

    def test_transitive_mcp_collected_through_lockfile(self, tmp_path):
        os.chdir(tmp_path)
        apm_modules = tmp_path / "apm_modules"

        # squad-alpha has no MCP, depends on infra-cloud
        _make_pkg_dir(apm_modules, "acme/squad-alpha", apm_deps=["acme/infra-cloud"])

        # infra-cloud declares two MCP servers
        _make_pkg_dir(
            apm_modules,
            "acme/infra-cloud",
            mcp=[
                "ghcr.io/acme/mcp-server-alpha",
                "ghcr.io/acme/mcp-server-beta",
            ],
        )

        # Lockfile records both packages
        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/squad-alpha", depth=1, resolved_by="root"),
                LockedDependency(
                    repo_url="acme/infra-cloud", depth=2, resolved_by="acme/squad-alpha"
                ),
            ],
        )

        result = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        names = [d.name for d in result]
        assert "ghcr.io/acme/mcp-server-alpha" in names
        assert "ghcr.io/acme/mcp-server-beta" in names

    def test_orphan_pkg_mcp_not_collected(self, tmp_path):
        """A package in apm_modules but NOT in the lockfile should be ignored."""
        os.chdir(tmp_path)
        apm_modules = tmp_path / "apm_modules"

        _make_pkg_dir(apm_modules, "acme/squad-alpha")
        _make_pkg_dir(apm_modules, "acme/orphan-pkg", mcp=["ghcr.io/acme/orphan-server"])

        # Only squad-alpha is locked
        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/squad-alpha", depth=1, resolved_by="root"),
            ],
        )

        result = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        names = [d.name for d in result]
        assert "ghcr.io/acme/orphan-server" not in names


# ---------------------------------------------------------------------------
# Scenario 1b — Deep transitive chain (3+ levels)
# ---------------------------------------------------------------------------
class TestDeepTransitiveChainMCP:
    """MCP servers declared at depth 4 (A → B → C → D) must be collected
    when only A is locked at depth 1.  Exercises the full recursive walk."""

    def setup_method(self):
        self._orig_cwd = os.getcwd()

    def teardown_method(self):
        os.chdir(self._orig_cwd)

    def test_depth_four_mcp_collected(self, tmp_path):
        os.chdir(tmp_path)
        apm_modules = tmp_path / "apm_modules"

        # A → B → C → D (D has MCP)
        _make_pkg_dir(apm_modules, "acme/pkg-a", apm_deps=["acme/pkg-b"])
        _make_pkg_dir(apm_modules, "acme/pkg-b", apm_deps=["acme/pkg-c"])
        _make_pkg_dir(apm_modules, "acme/pkg-c", apm_deps=["acme/pkg-d"])
        _make_pkg_dir(
            apm_modules,
            "acme/pkg-d",
            mcp=[
                "ghcr.io/acme/mcp-deep-server",
            ],
        )

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/pkg-a", depth=1, resolved_by="root"),
                LockedDependency(repo_url="acme/pkg-b", depth=2, resolved_by="acme/pkg-a"),
                LockedDependency(repo_url="acme/pkg-c", depth=3, resolved_by="acme/pkg-b"),
                LockedDependency(repo_url="acme/pkg-d", depth=4, resolved_by="acme/pkg-c"),
            ],
        )

        result = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        names = [d.name for d in result]
        assert "ghcr.io/acme/mcp-deep-server" in names

    def test_mcp_at_every_level_collected(self, tmp_path):
        """Each level in the chain has its own MCP — all must appear."""
        os.chdir(tmp_path)
        apm_modules = tmp_path / "apm_modules"

        _make_pkg_dir(
            apm_modules, "acme/pkg-a", apm_deps=["acme/pkg-b"], mcp=["ghcr.io/acme/mcp-level-1"]
        )
        _make_pkg_dir(
            apm_modules, "acme/pkg-b", apm_deps=["acme/pkg-c"], mcp=["ghcr.io/acme/mcp-level-2"]
        )
        _make_pkg_dir(apm_modules, "acme/pkg-c", mcp=["ghcr.io/acme/mcp-level-3"])

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/pkg-a", depth=1, resolved_by="root"),
                LockedDependency(repo_url="acme/pkg-b", depth=2, resolved_by="acme/pkg-a"),
                LockedDependency(repo_url="acme/pkg-c", depth=3, resolved_by="acme/pkg-b"),
            ],
        )

        result = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        names = [d.name for d in result]
        assert "ghcr.io/acme/mcp-level-1" in names
        assert "ghcr.io/acme/mcp-level-2" in names
        assert "ghcr.io/acme/mcp-level-3" in names


# ---------------------------------------------------------------------------
# Scenario 1c — Diamond dependency (A → B, A → C, B → D, C → D)
# ---------------------------------------------------------------------------
class TestDiamondDependencyMCP:
    """When two branches of the tree converge on the same leaf (diamond),
    MCP servers from the shared leaf must appear exactly once."""

    def setup_method(self):
        self._orig_cwd = os.getcwd()

    def teardown_method(self):
        os.chdir(self._orig_cwd)

    def test_diamond_mcp_collected_once(self, tmp_path):
        os.chdir(tmp_path)
        apm_modules = tmp_path / "apm_modules"

        # A → B, A → C, B → D, C → D
        _make_pkg_dir(apm_modules, "acme/pkg-a", apm_deps=["acme/pkg-b", "acme/pkg-c"])
        _make_pkg_dir(apm_modules, "acme/pkg-b", apm_deps=["acme/pkg-d"])
        _make_pkg_dir(apm_modules, "acme/pkg-c", apm_deps=["acme/pkg-d"])
        _make_pkg_dir(
            apm_modules,
            "acme/pkg-d",
            mcp=[
                "ghcr.io/acme/mcp-shared-server",
            ],
        )

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/pkg-a", depth=1, resolved_by="root"),
                LockedDependency(repo_url="acme/pkg-b", depth=2, resolved_by="acme/pkg-a"),
                LockedDependency(repo_url="acme/pkg-c", depth=2, resolved_by="acme/pkg-a"),
                LockedDependency(repo_url="acme/pkg-d", depth=3, resolved_by="acme/pkg-b"),
            ],
        )

        result = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        names = [d.name for d in result]
        assert "ghcr.io/acme/mcp-shared-server" in names

        # After dedup, exactly one entry
        merged = MCPIntegrator.deduplicate(result)
        merged_names = [d.name for d in merged]
        assert merged_names.count("ghcr.io/acme/mcp-shared-server") == 1

    def test_diamond_multiple_mcp_from_branches(self, tmp_path):
        """Each branch also contributes its own MCP — all must be present."""
        os.chdir(tmp_path)
        apm_modules = tmp_path / "apm_modules"

        _make_pkg_dir(apm_modules, "acme/pkg-a", apm_deps=["acme/pkg-b", "acme/pkg-c"])
        _make_pkg_dir(
            apm_modules, "acme/pkg-b", apm_deps=["acme/pkg-d"], mcp=["ghcr.io/acme/mcp-branch-b"]
        )
        _make_pkg_dir(
            apm_modules, "acme/pkg-c", apm_deps=["acme/pkg-d"], mcp=["ghcr.io/acme/mcp-branch-c"]
        )
        _make_pkg_dir(apm_modules, "acme/pkg-d", mcp=["ghcr.io/acme/mcp-leaf"])

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/pkg-a", depth=1, resolved_by="root"),
                LockedDependency(repo_url="acme/pkg-b", depth=2, resolved_by="acme/pkg-a"),
                LockedDependency(repo_url="acme/pkg-c", depth=2, resolved_by="acme/pkg-a"),
                LockedDependency(repo_url="acme/pkg-d", depth=3, resolved_by="acme/pkg-b"),
            ],
        )

        result = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        merged = MCPIntegrator.deduplicate(result)
        names = [d.name for d in merged]
        assert "ghcr.io/acme/mcp-branch-b" in names
        assert "ghcr.io/acme/mcp-branch-c" in names
        assert "ghcr.io/acme/mcp-leaf" in names
        assert len(names) == 3


# ---------------------------------------------------------------------------
# Scenario 2 — Uninstall removes transitive MCP servers
# ---------------------------------------------------------------------------
class TestUninstallRemovesTransitiveMCP:
    """After uninstalling a package, _remove_stale_mcp_servers should remove
    MCP entries that are no longer referenced by any remaining package."""

    def setup_method(self):
        self._orig_cwd = os.getcwd()

    def teardown_method(self):
        os.chdir(self._orig_cwd)

    def test_stale_servers_removed_from_mcp_json(self, tmp_path):
        os.chdir(tmp_path)

        # Pre-existing .vscode/mcp.json with servers from two packages
        mcp_json = tmp_path / ".vscode" / "mcp.json"
        _write_mcp_json(
            mcp_json,
            {
                "ghcr.io/acme/mcp-server-alpha": {"command": "npx", "args": ["alpha"]},
                "ghcr.io/acme/mcp-server-beta": {"command": "npx", "args": ["beta"]},
                "ghcr.io/acme/mcp-server-gamma": {"command": "npx", "args": ["gamma"]},
            },
        )

        # Suppose infra-cloud (alpha + beta) was uninstalled, gamma remains.
        old_servers = {
            "ghcr.io/acme/mcp-server-alpha",
            "ghcr.io/acme/mcp-server-beta",
            "ghcr.io/acme/mcp-server-gamma",
        }
        new_servers = {"ghcr.io/acme/mcp-server-gamma"}
        stale = old_servers - new_servers

        MCPIntegrator.remove_stale(stale, runtime="vscode")

        updated = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert "ghcr.io/acme/mcp-server-alpha" not in updated["servers"]
        assert "ghcr.io/acme/mcp-server-beta" not in updated["servers"]
        assert "ghcr.io/acme/mcp-server-gamma" in updated["servers"]

    def test_lockfile_mcp_list_updated_after_uninstall(self, tmp_path):
        os.chdir(tmp_path)

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/base-lib", depth=1, resolved_by="root"),
            ],
            mcp_servers=["ghcr.io/acme/mcp-server-alpha", "ghcr.io/acme/mcp-server-beta"],
        )

        # After uninstall, only beta remains
        MCPIntegrator.update_lockfile({"ghcr.io/acme/mcp-server-beta"})

        reloaded = LockFile.read(lock_path)
        assert reloaded.mcp_servers == ["ghcr.io/acme/mcp-server-beta"]

    def test_lockfile_mcp_cleared_when_all_removed(self, tmp_path):
        os.chdir(tmp_path)

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/base-lib", depth=1, resolved_by="root"),
            ],
            mcp_servers=["ghcr.io/acme/mcp-server-alpha"],
        )

        MCPIntegrator.update_lockfile(set())

        reloaded = LockFile.read(lock_path)
        assert reloaded.mcp_servers == []


# ---------------------------------------------------------------------------
# Scenario 3 — Update with MCP rename (stale removed, new present)
# ---------------------------------------------------------------------------
class TestUpdateMCPRename:
    """When a dependency renames an MCP server between versions, the stale
    name must be removed and the new name must be present."""

    def setup_method(self):
        self._orig_cwd = os.getcwd()

    def teardown_method(self):
        os.chdir(self._orig_cwd)

    def test_rename_produces_correct_stale_set(self, tmp_path):
        os.chdir(tmp_path)

        # Before update: lockfile knows about the old server name
        old_mcp = {"ghcr.io/acme/mcp-server-old", "ghcr.io/acme/mcp-server-gamma"}

        # After update: the package now declares a renamed server
        apm_modules = tmp_path / "apm_modules"
        _make_pkg_dir(
            apm_modules,
            "acme/infra-cloud",
            mcp=[
                "ghcr.io/acme/mcp-server-new",  # renamed from mcp-server-old
                "ghcr.io/acme/mcp-server-gamma",
            ],
        )

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/infra-cloud", depth=1, resolved_by="root"),
            ],
            mcp_servers=sorted(old_mcp),
        )

        transitive = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        new_mcp = MCPIntegrator.get_server_names(transitive)
        stale = old_mcp - new_mcp

        assert "ghcr.io/acme/mcp-server-old" in stale
        assert "ghcr.io/acme/mcp-server-new" not in stale
        assert "ghcr.io/acme/mcp-server-new" in new_mcp
        assert "ghcr.io/acme/mcp-server-gamma" in new_mcp

    def test_rename_removes_stale_from_mcp_json(self, tmp_path):
        os.chdir(tmp_path)

        mcp_json = tmp_path / ".vscode" / "mcp.json"
        _write_mcp_json(
            mcp_json,
            {
                "ghcr.io/acme/mcp-server-old": {"command": "npx", "args": ["old"]},
                "ghcr.io/acme/mcp-server-gamma": {"command": "npx", "args": ["gamma"]},
            },
        )

        MCPIntegrator.remove_stale({"ghcr.io/acme/mcp-server-old"}, runtime="vscode")

        updated = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert "ghcr.io/acme/mcp-server-old" not in updated["servers"]
        assert "ghcr.io/acme/mcp-server-gamma" in updated["servers"]


# ---------------------------------------------------------------------------
# Scenario 4 — Update with MCP removal
# ---------------------------------------------------------------------------
class TestUpdateMCPRemoval:
    """When a dependency drops an MCP server entirely, the server must be
    removed from both .vscode/mcp.json and the lockfile."""

    def setup_method(self):
        self._orig_cwd = os.getcwd()

    def teardown_method(self):
        os.chdir(self._orig_cwd)

    def test_removed_mcp_detected_as_stale(self, tmp_path):
        os.chdir(tmp_path)

        old_mcp = {"ghcr.io/acme/mcp-server-alpha", "ghcr.io/acme/mcp-server-beta"}

        # After update, the package no longer declares any MCP servers
        apm_modules = tmp_path / "apm_modules"
        _make_pkg_dir(apm_modules, "acme/infra-cloud")  # no mcp arg

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/infra-cloud", depth=1, resolved_by="root"),
            ],
            mcp_servers=sorted(old_mcp),
        )

        transitive = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        new_mcp = MCPIntegrator.get_server_names(transitive)
        stale = old_mcp - new_mcp

        assert stale == old_mcp  # all old servers are stale

    def test_removal_cleans_mcp_json_and_lockfile(self, tmp_path):
        os.chdir(tmp_path)

        mcp_json = tmp_path / ".vscode" / "mcp.json"
        _write_mcp_json(
            mcp_json,
            {
                "ghcr.io/acme/mcp-server-alpha": {"command": "npx", "args": ["alpha"]},
            },
        )

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/infra-cloud", depth=1, resolved_by="root"),
            ],
            mcp_servers=["ghcr.io/acme/mcp-server-alpha"],
        )

        MCPIntegrator.remove_stale({"ghcr.io/acme/mcp-server-alpha"}, runtime="vscode")
        MCPIntegrator.update_lockfile(set())

        updated = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert updated["servers"] == {}

        reloaded = LockFile.read(lock_path)
        assert reloaded.mcp_servers == []


# ---------------------------------------------------------------------------
# Scenario 5 — Deduplication across root and transitive MCP
# ---------------------------------------------------------------------------
class TestDeduplicationRootAndTransitive:
    """Root-declared MCP deps take precedence over transitive ones.
    Dedup must collapse duplicates while keeping root declarations first."""

    def test_root_overrides_transitive_duplicate(self, tmp_path):
        apm_modules = tmp_path / "apm_modules"
        _make_pkg_dir(
            apm_modules,
            "acme/infra-cloud",
            mcp=[
                "ghcr.io/acme/mcp-server-alpha",
            ],
        )

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/infra-cloud", depth=1, resolved_by="root"),
            ],
        )

        # Root declares alpha with extra config (dict form)
        root_mcp = [
            {
                "name": "ghcr.io/acme/mcp-server-alpha",
                "type": "http",
                "url": "https://custom.example.com",
            }
        ]
        transitive_mcp = MCPIntegrator.collect_transitive(apm_modules, lock_path)

        merged = MCPIntegrator.deduplicate(root_mcp + transitive_mcp)
        assert len(merged) == 1
        # Root's dict form should win (first occurrence)
        assert isinstance(merged[0], dict)
        assert merged[0]["url"] == "https://custom.example.com"

    def test_dedup_preserves_distinct_servers(self, tmp_path):
        apm_modules = tmp_path / "apm_modules"
        _make_pkg_dir(apm_modules, "acme/infra-cloud", mcp=["ghcr.io/acme/mcp-server-alpha"])
        _make_pkg_dir(apm_modules, "acme/base-lib", mcp=["ghcr.io/acme/mcp-server-beta"])

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/infra-cloud", depth=1, resolved_by="root"),
                LockedDependency(repo_url="acme/base-lib", depth=2, resolved_by="acme/infra-cloud"),
            ],
        )

        transitive_mcp = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        merged = MCPIntegrator.deduplicate(transitive_mcp)
        names = [d.name for d in merged]
        assert len(names) == 2
        assert "ghcr.io/acme/mcp-server-alpha" in names
        assert "ghcr.io/acme/mcp-server-beta" in names


# ---------------------------------------------------------------------------
# Scenario 6 — Virtual-path packages in lockfile
# ---------------------------------------------------------------------------
class TestVirtualPathMCPCollection:
    """Packages with virtual_path in the lockfile must be correctly resolved
    to their subdirectory inside apm_modules."""

    def test_virtual_path_mcp_collected(self, tmp_path):
        apm_modules = tmp_path / "apm_modules"

        # Virtual package: acme/monorepo with virtual_path=packages/web-api
        _make_pkg_dir(
            apm_modules,
            "acme/monorepo",
            virtual_path="packages/web-api",
            name="web-api",
            mcp=["ghcr.io/acme/mcp-server-web"],
        )

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(
                    repo_url="acme/monorepo",
                    virtual_path="packages/web-api",
                    is_virtual=True,
                    depth=1,
                    resolved_by="root",
                ),
            ],
        )

        result = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        names = [d.name for d in result]
        assert "ghcr.io/acme/mcp-server-web" in names

    def test_virtual_and_non_virtual_together(self, tmp_path):
        apm_modules = tmp_path / "apm_modules"

        _make_pkg_dir(apm_modules, "acme/base-lib", mcp=["ghcr.io/acme/mcp-base"])
        _make_pkg_dir(
            apm_modules,
            "acme/monorepo",
            virtual_path="packages/api",
            name="api",
            mcp=["ghcr.io/acme/mcp-api"],
        )

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/base-lib", depth=1, resolved_by="root"),
                LockedDependency(
                    repo_url="acme/monorepo",
                    virtual_path="packages/api",
                    is_virtual=True,
                    depth=1,
                    resolved_by="root",
                ),
            ],
        )

        result = MCPIntegrator.collect_transitive(apm_modules, lock_path)
        names = [d.name for d in result]
        assert len(names) == 2
        assert "ghcr.io/acme/mcp-base" in names
        assert "ghcr.io/acme/mcp-api" in names


# ---------------------------------------------------------------------------
# Scenario 7 — Self-defined MCP trust_private gating
# ---------------------------------------------------------------------------
class TestSelfDefinedMCPTrustGating:
    """Self-defined (non-registry) MCP servers from transitive packages are
    gated behind trust_private.  Direct dependencies (depth=1) are auto-trusted."""

    def test_self_defined_skipped_for_transitive(self, tmp_path):
        apm_modules = tmp_path / "apm_modules"
        _make_pkg_dir(
            apm_modules,
            "acme/infra-cloud",
            mcp=[
                "ghcr.io/acme/mcp-registry-server",
                {
                    "name": "private-srv",
                    "registry": False,
                    "transport": "http",
                    "url": "https://private.example.com",
                },
            ],
        )

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/infra-cloud", depth=2, resolved_by="some-dep"),
            ],
        )

        result = MCPIntegrator.collect_transitive(apm_modules, lock_path, trust_private=False)
        names = [d.name for d in result]
        assert "ghcr.io/acme/mcp-registry-server" in names
        assert "private-srv" not in names

    def test_direct_dep_self_defined_auto_trusted(self, tmp_path):
        """Depth=1 packages have their self-defined MCPs auto-trusted."""
        apm_modules = tmp_path / "apm_modules"
        _make_pkg_dir(
            apm_modules,
            "acme/infra-cloud",
            mcp=[
                "ghcr.io/acme/mcp-registry-server",
                {
                    "name": "private-srv",
                    "registry": False,
                    "transport": "http",
                    "url": "https://private.example.com",
                },
            ],
        )

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/infra-cloud", depth=1, resolved_by="root"),
            ],
        )

        result = MCPIntegrator.collect_transitive(apm_modules, lock_path, trust_private=False)
        names = [d.name for d in result]
        assert "ghcr.io/acme/mcp-registry-server" in names
        assert "private-srv" in names

    def test_self_defined_included_when_trusted(self, tmp_path):
        apm_modules = tmp_path / "apm_modules"
        _make_pkg_dir(
            apm_modules,
            "acme/infra-cloud",
            mcp=[
                "ghcr.io/acme/mcp-registry-server",
                {
                    "name": "private-srv",
                    "registry": False,
                    "transport": "http",
                    "url": "https://private.example.com",
                },
            ],
        )

        lock_path = tmp_path / "apm.lock.yaml"
        _write_lockfile(
            lock_path,
            [
                LockedDependency(repo_url="acme/infra-cloud", depth=1, resolved_by="root"),
            ],
        )

        result = MCPIntegrator.collect_transitive(apm_modules, lock_path, trust_private=True)
        names = [d.name for d in result]
        assert "ghcr.io/acme/mcp-registry-server" in names
        assert "private-srv" in names


class TestStaleCleanupKeyNormalization:
    """_remove_stale_mcp_servers should match config keys that use only the
    last path segment (Copilot CLI, Codex) even when stale_names contains
    full registry references with '/'."""

    def test_last_segment_removed_from_copilot_config(self, tmp_path, monkeypatch):
        """Stale name 'io.github.github/github-mcp-server' should remove
        config key 'github-mcp-server' from Copilot CLI config."""
        monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        copilot_dir = tmp_path / ".copilot"
        copilot_dir.mkdir()
        copilot_config = copilot_dir / "mcp-config.json"
        copilot_config.write_text(
            json.dumps(
                {"mcpServers": {"github-mcp-server": {"command": "npx", "args": ["mcp-server"]}}}
            )
        )

        stale = {"io.github.github/github-mcp-server"}
        MCPIntegrator.remove_stale(stale, runtime="copilot")

        result = json.loads(copilot_config.read_text())
        assert "github-mcp-server" not in result["mcpServers"]

    def test_full_ref_removed_from_vscode_config(self, tmp_path, monkeypatch):
        """VS Code uses the full reference as key — should still match."""
        monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        mcp_json = vscode_dir / "mcp.json"
        mcp_json.write_text(
            json.dumps({"servers": {"io.github.github/github-mcp-server": {"type": "stdio"}}})
        )

        stale = {"io.github.github/github-mcp-server"}
        MCPIntegrator.remove_stale(stale, runtime="vscode")

        result = json.loads(mcp_json.read_text())
        assert "io.github.github/github-mcp-server" not in result["servers"]

    def test_short_name_without_slash_still_works(self, tmp_path, monkeypatch):
        """A stale name without '/' (e.g. 'acme-kb') should still match directly."""
        monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        copilot_dir = tmp_path / ".copilot"
        copilot_dir.mkdir()
        copilot_config = copilot_dir / "mcp-config.json"
        copilot_config.write_text(
            json.dumps({"mcpServers": {"acme-kb": {"command": "npx", "args": ["acme-kb"]}}})
        )

        stale = {"acme-kb"}
        MCPIntegrator.remove_stale(stale, runtime="copilot")

        result = json.loads(copilot_config.read_text())
        assert "acme-kb" not in result["mcpServers"]
