"""tests for apm_cli.commands.uninstall.engine (pure logic cases)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-skills-root-derivation", "P-OWN-orphan-package-selection"),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger():
    m = MagicMock()
    m.verbose = False
    return m


def _make_lockfile(deps=None):
    return LockFile(dependencies=deps or {})


def _make_locked_dep(key, resolved_by=None):
    return LockedDependency(repo_url=key, resolved_by=resolved_by)


@pytest.fixture(params=["locked", "mock"])
def dep_kind(request):
    """Run a test with real lockfile objects and with MagicMock stand-ins."""
    return request.param


@pytest.fixture
def make_lockfile(dep_kind):
    return _make_lockfile if dep_kind == "locked" else _make_mock_lockfile


@pytest.fixture
def make_locked_dep(dep_kind):
    return _make_locked_dep if dep_kind == "locked" else _make_mock_locked_dep


def test_reachability_verbose_diagnostics_redact_local_paths():
    """Local reachability failures never reveal declared machine paths."""
    from apm_cli.commands.uninstall.engine import _warn_reachability_incomplete

    local_path = r"C:\Users\runner\Private Package"
    reachability = SimpleNamespace(unverifiable=[(local_path, f"manifest missing at {local_path}")])
    logger = _make_logger()
    logger.verbose = True

    _warn_reachability_incomplete(reachability, logger)

    rendered = "\n".join(call.args[0] for call in logger.verbose_detail.call_args_list)
    assert local_path not in rendered
    assert "local dependency" in rendered


def test_reintegration_verbose_detail_redacts_only_local_dependency_paths():
    """Remote errors retain detail while local declarations remain private."""
    from apm_cli.commands.uninstall.engine import _reintegration_error_detail
    from apm_cli.models.dependency import DependencyReference

    remote = DependencyReference.parse("owner/repo")
    local_path = r"C:\Users\runner\Private Package"
    local = DependencyReference.parse(local_path)
    error = RuntimeError("failed at remote primitive")

    assert _reintegration_error_detail(remote, error) == (
        "RuntimeError: failed at remote primitive"
    )
    local_detail = _reintegration_error_detail(local, RuntimeError(local_path))
    assert local_detail == "RuntimeError"
    assert local_path not in local_detail


# ---------------------------------------------------------------------------
# _build_children_index
# ---------------------------------------------------------------------------


class TestBuildChildrenIndex:
    def test_empty_deps(self, make_lockfile):
        from apm_cli.install.orphans import _build_children_index

        lf = make_lockfile()
        result = _build_children_index(lf)
        assert result == {}

    def test_no_resolved_by_skipped(self, make_lockfile, make_locked_dep):
        from apm_cli.install.orphans import _build_children_index

        dep = make_locked_dep("org/child", resolved_by=None)
        lf = make_lockfile({"org/child": dep})
        result = _build_children_index(lf)
        assert result == {}

    def test_resolved_by_populated(self, make_lockfile, make_locked_dep):
        from apm_cli.install.orphans import _build_children_index

        parent_url = "org/parent"
        child = make_locked_dep("org/child", resolved_by=parent_url)
        lf = make_lockfile({"org/child": child})
        result = _build_children_index(lf)
        assert parent_url in result
        assert child in result[parent_url]


# ---------------------------------------------------------------------------
# _validate_uninstall_packages
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _dry_run_uninstall
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _remove_packages_from_disk
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _cleanup_transitive_orphans
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _resolve_marketplace_packages -- dry_run and registry paths
# ---------------------------------------------------------------------------


class TestResolveMarketplacePackages:
    def test_dry_run_skips_registry(self):
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = _make_logger()
        with patch(
            "apm_cli.marketplace.resolver.parse_marketplace_ref",
            return_value=("plugin", "market", None),
        ):
            result = _resolve_marketplace_packages(
                ["plugin@market"],
                None,
                logger,
                dry_run=True,
            )
        assert result["plugin@market"] is None
        logger.warning.assert_called()

    def test_registry_fallback_resolves_canonical(self, make_lockfile, make_locked_dep):
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = _make_logger()
        mock_resolution = MagicMock()
        mock_resolution.canonical = "org/repo"

        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("plugin", "market", None),
            ),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                return_value=mock_resolution,
            ),
        ):
            lf = make_lockfile({"org/repo": make_locked_dep("org/repo")})
            result = _resolve_marketplace_packages(
                ["plugin@market"],
                lf,
                logger,
            )
        assert result["plugin@market"] == "org/repo"

    def test_registry_fallback_not_in_lockfile_refused(self, make_lockfile, make_locked_dep):
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = _make_logger()
        mock_resolution = MagicMock()
        mock_resolution.canonical = "org/other-repo"

        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("plugin", "market", None),
            ),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                return_value=mock_resolution,
            ),
        ):
            lf = make_lockfile({"org/repo": make_locked_dep("org/repo")})
            result = _resolve_marketplace_packages(
                ["plugin@market"],
                lf,
                logger,
            )
        assert result["plugin@market"] is None
        logger.warning.assert_called()

    def test_registry_fallback_exception_logged(self):
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = _make_logger()

        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("plugin", "market", None),
            ),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                side_effect=Exception("network error"),
            ),
        ):
            result = _resolve_marketplace_packages(
                ["plugin@market"],
                None,
                logger,
            )
        assert result["plugin@market"] is None
        logger.warning.assert_called()

    def test_no_lockfile_trusts_registry(self):
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = _make_logger()
        mock_resolution = MagicMock()
        mock_resolution.canonical = "org/repo"

        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("plugin", "market", None),
            ),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                return_value=mock_resolution,
            ),
        ):
            result = _resolve_marketplace_packages(
                ["plugin@market"],
                None,  # No lockfile
                logger,
            )
        assert result["plugin@market"] == "org/repo"


def _make_mock_lockfile(deps=None):
    lf = MagicMock()
    lf.replacements = None
    lf.dependencies = deps or {}
    lf.get_package_dependencies.return_value = list((deps or {}).values())
    return lf


def _make_mock_locked_dep(key, repo_url=None, resolved_by=None):
    dep = MagicMock()
    dep.get_unique_key.return_value = key
    dep.repo_url = repo_url or key.split("/")[-1]
    dep.resolved_by = resolved_by
    dep.parent_keys = (resolved_by,)
    return dep


# ---------------------------------------------------------------------------
# Targets uninstall reconciles after a target change (#4636)
# ---------------------------------------------------------------------------


def _profiles(*names):
    from apm_cli.integration.targets import KNOWN_TARGETS

    return [KNOWN_TARGETS[name] for name in names]


def _locked_with(files=(), manifest_targets=None):
    lockfile = LockFile(dependencies={"acme/a": LockedDependency(repo_url="acme/a")})
    lockfile.dependencies["acme/a"].deployed_files = list(files)
    lockfile.manifest_targets = manifest_targets
    return lockfile


_DEPLOYMENT_ROW = """lockfile_version: '1'
dependencies:
- repo_url: acme/a
  deployed_files:
  - {value}
deployments:
- kind: project-relative
  target: {target}
  value: {value}
  runtime: null
  scope: project
  owners:
  - acme/a
  active_owner: acme/a
  content_hash: null
"""


def _recorded(target, value):
    return LockFile.from_yaml(_DEPLOYMENT_ROW.format(target=target, value=value))


@pytest.mark.parametrize(
    ("declared", "files", "kept", "new"),
    [
        (("claude",), [".github/prompts/a.prompt.md"], [], ["claude"]),
        (("copilot", "claude"), [".github/prompts/a.prompt.md"], ["copilot"], ["claude"]),
        (
            ("copilot", "claude", "gemini"),
            [".github/prompts/a.prompt.md", ".claude/commands/a.md"],
            ["copilot", "claude"],
            ["gemini"],
        ),
        (("copilot",), [".github/prompts/a.prompt.md", ".claude/commands/a.md"], ["copilot"], []),
        (("copilot",), [".github/prompts/a.prompt.md"], ["copilot"], []),
    ],
)
def test_uninstall_reconciles_only_targets_the_lock_declares(declared, files, kept, new):
    from apm_cli.commands.uninstall.recorded_targets import lock_recorded_targets

    targets, added = lock_recorded_targets(_profiles(*declared), _locked_with(files=files))
    assert [target.name for target in targets] == kept
    assert added == new


def test_a_refreshed_declaration_is_not_deployment_evidence():
    """A changed uninstall write records apm.yml's new targets in manifest_targets."""
    from apm_cli.commands.uninstall.recorded_targets import lock_recorded_targets

    lockfile = _locked_with(files=[".github/prompts/a.prompt.md"], manifest_targets=["claude"])
    targets, added = lock_recorded_targets(_profiles("claude"), lockfile)
    assert (targets, added) == ([], ["claude"])


def test_without_recorded_declarations_a_target_needs_a_recorded_file():
    from apm_cli.commands.uninstall.recorded_targets import lock_recorded_targets

    lockfile = _locked_with(files=[".claude/skills/a/SKILL.md"])
    targets, added = lock_recorded_targets(_profiles("copilot", "claude"), lockfile)
    assert [target.name for target in targets] == ["claude"]
    assert added == ["copilot"]


@pytest.mark.parametrize(
    ("declared", "kept"),
    [(("copilot",), ["copilot"]), (("copilot", "gemini"), [])],
)
def test_a_legacy_shared_root_row_counts_only_for_its_one_governing_target(declared, kept):
    from apm_cli.commands.uninstall.recorded_targets import lock_recorded_targets

    lockfile = _locked_with(files=[".agents/skills/a/SKILL.md"])
    targets, _added = lock_recorded_targets(_profiles(*declared), lockfile)
    assert [target.name for target in targets] == kept


def test_a_deployment_row_names_its_target_on_a_shared_root():
    from apm_cli.commands.uninstall.recorded_targets import lock_recorded_targets

    lockfile = _recorded("copilot", ".agents/skills/a/SKILL.md")
    targets, added = lock_recorded_targets(_profiles("copilot", "gemini"), lockfile)
    assert [target.name for target in targets] == ["copilot"]
    assert added == ["gemini"]


@pytest.mark.parametrize("lockfile", [None, LockFile()])
def test_without_any_lock_record_every_target_is_reconciled(lockfile):
    from apm_cli.commands.uninstall.recorded_targets import lock_recorded_targets

    targets, added = lock_recorded_targets(_profiles("copilot", "claude"), lockfile)
    assert [target.name for target in targets] == ["copilot", "claude"]
    assert added == []


def test_the_new_target_advice_names_apm_install():
    from apm_cli.core.remediation import UNINSTALL_NEW_TARGET_UNDEPLOYED

    assert UNINSTALL_NEW_TARGET_UNDEPLOYED.bind(targets="claude").sentence() == (
        "Run 'apm install' to deploy the remaining packages to claude and record them "
        "in apm.lock.yaml."
    )


@pytest.mark.parametrize(
    ("hook_entry", "noticed"),
    [
        pytest.param(("not approved", ()), True, id="bin-only-native-no-kinds"),
        pytest.param(("not approved", ("hooks",)), False, id="hooks-already-named"),
    ],
)
def test_native_survivor_notice_follows_named_kinds(hook_entry, noticed):
    """A native survivor whose hook retirement named no kind still gets its notice (#4567)."""
    from apm_cli.native_plugins.uninstall import retire_disallowed_native_survivors

    key = "acme/plugin"
    lockfile = SimpleNamespace(dependencies={key: SimpleNamespace(native_plugin=object())})
    retirements = SimpleNamespace(denied_packages={}, native_packages={key: "not approved"})
    logger = MagicMock()

    retired = retire_disallowed_native_survivors(
        lockfile, retirements, set(), {key: hook_entry}, logger
    )

    assert retired == {key}
    assert lockfile.dependencies[key].native_plugin is None
    notice = f"{key} native plugin is no longer allowed (not approved); removing it"
    warnings = [call.args[0] for call in logger.warning.call_args_list]
    assert (notice in warnings) is noticed


def test_prune_names_an_empty_kind_native_survivor_once(monkeypatch, tmp_path):
    """Prune's fallback notice skips a native survivor the helper already named (#4567)."""
    from apm_cli.native_plugins.uninstall import retire_gated_native_plugins

    key = "acme/plugin"
    lockfile = SimpleNamespace(dependencies={key: SimpleNamespace(native_plugin=object())})
    retirements = SimpleNamespace(
        denied_packages={},
        native_packages={key: "not approved"},
        hook_packages={key: ("not approved", ())},
    )
    monkeypatch.setattr("apm_cli.install.exec_gate.gate_retirements", lambda *a, **k: retirements)
    monkeypatch.setattr(
        "apm_cli.models.apm_package.APMPackage.from_apm_yml", lambda *_a, **_k: object()
    )
    logger = MagicMock()

    assert retire_gated_native_plugins(lockfile, tmp_path, tmp_path / "apm.yml", tmp_path, logger)

    warnings = [call.args[0] for call in logger.warning.call_args_list]
    assert warnings == [f"{key} native plugin is no longer allowed (not approved); removing it"]
