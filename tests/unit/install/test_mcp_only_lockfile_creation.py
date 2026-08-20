"""Regression tests for lockfile creation in MCP-only projects.

Issue #2373: an ``apm.yml`` declaring only ``dependencies.mcp`` never enters
the APM install pipeline -- the gate in ``commands/install.py`` requires APM
dependencies, local ``.apm/`` primitives, or orphan lockfile entries -- so
nothing created ``apm.lock.yaml``. ``MCPIntegrator.update_lockfile`` returned
early on a missing file, and ``apm audit --ci`` counts MCP dependencies when
deciding a lockfile is required, so a successful install was immediately
followed by "Lockfile missing -- run 'apm install'".

The MCP integration owns MCP state persistence, so it establishes the
lockfile when it has state to record -- while calls that clear the last
server must still not create one for a project that never had it.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.apm_package import APMPackage
from apm_cli.policy.ci_checks import _check_lockfile_exists

MCP_ONLY_APM_YML = """\
name: jira
version: 1.0.0
description: Adds Atlassian MCP
targets:
  - claude
  - copilot
dependencies:
  mcp:
    - name: com.atlassian/atlassian-mcp-server
"""

SERVER = "atlassian-mcp-server"
CONFIGS = {SERVER: {"command": "npx", "args": ["-y", "atlassian-mcp"]}}


class _ProjectCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.lock_path = get_lockfile_path(self.root)

    def _persist(self, servers=frozenset({SERVER}), configs=None):
        MCPIntegrator.update_lockfile(
            set(servers),
            self.lock_path,
            mcp_configs=CONFIGS if configs is None else configs,
            mcp_target_servers={"claude": sorted(servers)} if servers else {},
            mcp_config_provenance={},
        )


class TestLockfileCreatedForMcpOnlyProject(_ProjectCase):
    def test_lockfile_is_created_with_the_mcp_state(self):
        self._persist()
        self.assertTrue(self.lock_path.exists(), "MCP install must leave a lockfile")
        lock = LockFile.read(self.lock_path)
        self.assertEqual(lock.mcp_servers, [SERVER])
        self.assertEqual(lock.mcp_configs, CONFIGS)

    def test_created_lockfile_records_the_apm_version(self):
        """Downstream version gating reads this field; it must not be blank."""
        self._persist()
        self.assertTrue(LockFile.read(self.lock_path).apm_version)

    def test_created_lockfile_has_no_synthetic_dependencies(self):
        self._persist()
        self.assertEqual(dict(LockFile.read(self.lock_path).dependencies), {})

    def test_audit_lockfile_check_passes_after_install(self):
        """The reported symptom: audit failed right after a successful install."""
        (self.root / "apm.yml").write_text(MCP_ONLY_APM_YML, encoding="utf-8")
        manifest = APMPackage.from_apm_yml(self.root / "apm.yml")

        before = _check_lockfile_exists(self.root, manifest)
        self.assertFalse(before.passed, "fixture sanity: audit must fail without a lockfile")

        self._persist()

        after = _check_lockfile_exists(self.root, manifest)
        self.assertTrue(after.passed, after.message)

    def test_configs_without_server_names_still_create_the_lockfile(self):
        self._persist(servers=frozenset())
        self.assertTrue(self.lock_path.exists())

    def test_creation_failure_emits_actionable_warning(self):
        with (
            patch.object(LockFile, "save", side_effect=OSError("read-only")),
            patch("apm_cli.integration.mcp_integrator._rich_warning") as warning,
            self.assertRaises(OSError),
        ):
            self._persist()

        warning.assert_called_once()
        self.assertIn("Could not write", warning.call_args.args[0])
        self.assertIn("Ensure the directory is writable", warning.call_args.args[0])


class TestLegacyLockfileIsMigratedFirst(_ProjectCase):
    """`apm install --mcp` must not shadow a legacy ``apm.lock``.

    Creating ``apm.lock.yaml`` before the rename would make
    ``migrate_lockfile_if_needed`` skip it forever, dropping the legacy
    file's pinned dependencies.
    """

    def test_legacy_lockfile_is_renamed_before_the_path_is_resolved(self):
        from apm_cli.deps.lockfile import LEGACY_LOCKFILE_NAME, migrate_lockfile_if_needed

        legacy = self.root / LEGACY_LOCKFILE_NAME
        lock = LockFile(apm_version="0.0.1-test")
        lock.save(legacy)
        self.assertTrue(legacy.exists())

        # What run_mcp_install now does before resolving the lock path.
        migrate_lockfile_if_needed(self.root)

        self.assertTrue(self.lock_path.exists(), "legacy lockfile must be migrated")
        self.assertFalse(legacy.exists(), "legacy lockfile must not linger")

        # Persisting MCP state afterwards updates the migrated file rather
        # than creating a second one.
        self._persist()
        self.assertEqual(LockFile.read(self.lock_path).mcp_servers, [SERVER])


class TestFullAuditBaselinePasses(unittest.TestCase):
    """The acceptance criterion is `apm audit --ci`, not one check in isolation.

    Asserting only ``lockfile-exists`` would let the fix move the failure to
    the next check -- the user would still be told to run ``apm install``
    immediately after a successful one.
    """

    SELF_DEFINED_APM_YML = """\
name: jira
version: 1.0.0
description: Self-defined MCP server
targets:
  - copilot
dependencies:
  mcp:
    - name: my-server
      registry: false
      transport: stdio
      command: node
      args: ["server.js"]
"""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "apm.yml").write_text(self.SELF_DEFINED_APM_YML, encoding="utf-8")
        self.manifest = APMPackage.from_apm_yml(self.root / "apm.yml")
        self.lock_path = get_lockfile_path(self.root)

    def _derived_configs(self) -> dict:
        """Derive configs the way ``run_mcp_integration`` does.

        Both it and the audit's config-consistency check go through
        ``CurrentMcpConfigView``, so persisting anything else would pass this
        test while leaving a real install inconsistent.
        """
        from apm_cli.constants import APM_MODULES_DIR
        from apm_cli.integration.mcp_config_view import CurrentMcpConfigView

        view = CurrentMcpConfigView.derive(
            self.manifest,
            None,
            self.root / APM_MODULES_DIR,
            trust_transitive_self_defined=True,
        )
        return dict(view.configs)

    def _run_audit(self):
        from apm_cli.policy.ci_checks import run_baseline_checks

        return run_baseline_checks(self.root, fail_fast=False, ci_mode=True)

    def test_audit_fails_before_install(self):
        result = self._run_audit()
        self.assertFalse(result.passed, "fixture sanity: audit must fail without a lockfile")

    def test_every_baseline_check_passes_after_install(self):
        configs = self._derived_configs()
        MCPIntegrator.update_lockfile(
            set(configs),
            self.lock_path,
            mcp_configs=configs,
            mcp_target_servers={"copilot": sorted(configs)},
            mcp_config_provenance={},
        )

        result = self._run_audit()
        failed = [f"{c.name}: {c.message}" for c in result.checks if not c.passed]
        self.assertEqual(failed, [], f"audit must be clean after install; failures: {failed}")
        self.assertTrue(result.passed)

    def test_single_server_install_shape_also_passes(self):
        """`apm install mcp <server>` persists configs without target servers."""
        configs = self._derived_configs()
        MCPIntegrator.update_lockfile(set(configs), self.lock_path, mcp_configs=configs)

        result = self._run_audit()
        failed = [f"{c.name}: {c.message}" for c in result.checks if not c.passed]
        self.assertEqual(failed, [], f"audit must be clean; failures: {failed}")


class TestNoLockfileConjuredWithoutState(_ProjectCase):
    def test_clearing_the_last_server_does_not_create_a_lockfile(self):
        """Removing MCP state from a project that never had a lockfile is a no-op."""
        MCPIntegrator.update_lockfile(
            set(),
            self.lock_path,
            mcp_configs={},
            mcp_target_servers={},
            mcp_config_provenance={},
        )
        self.assertFalse(self.lock_path.exists())

    def test_no_state_at_all_does_not_create_a_lockfile(self):
        MCPIntegrator.update_lockfile(set(), self.lock_path)
        self.assertFalse(self.lock_path.exists())


class TestExistingLockfileBehaviourUnchanged(_ProjectCase):
    """Guards for the pre-existing update path."""

    def test_repeated_identical_persist_does_not_rewrite(self):
        self._persist()
        first = self.lock_path.read_text(encoding="utf-8")
        self._persist()
        self.assertEqual(self.lock_path.read_text(encoding="utf-8"), first)

    def test_existing_lockfile_is_updated_not_replaced(self):
        lock = LockFile(apm_version="0.0.1-test")
        lock.lsp_servers = ["some-lsp"]
        lock.save(self.lock_path)

        self._persist()

        updated = LockFile.read(self.lock_path)
        self.assertEqual(updated.mcp_servers, [SERVER])
        self.assertEqual(updated.lsp_servers, ["some-lsp"], "unrelated state must survive")

    def test_clearing_servers_on_an_existing_lockfile_still_writes(self):
        self._persist()
        MCPIntegrator.update_lockfile(
            set(),
            self.lock_path,
            mcp_configs={},
            mcp_target_servers={},
            mcp_config_provenance={},
        )
        self.assertEqual(LockFile.read(self.lock_path).mcp_servers, [])


if __name__ == "__main__":
    unittest.main()
