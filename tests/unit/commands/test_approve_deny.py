"""Unit tests for ``apm_cli.commands.approve`` (apm approve / deny / explain).

Issue #1873 vocabulary unification: ``apm approve`` writes to the project
``apm.yml`` ``executables.allow`` block by DEFAULT (committed, admin UX), and
to ``~/.apm/config.json`` only with ``--user`` (personal, lowest authority).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.commands.approve import (
    _find_matching_key,
    approve_cmd,
    deny_cmd,
    load_org_policy,
    scan_installed_executable_packages,
)
from apm_cli.commands.policy import policy as policy_group
from apm_cli.core.command_logger import CommandLogger
from apm_cli.install.errors import PolicyViolationError
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.schema import ApmPolicy, ExecutablesPolicy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_manifest(tmpdir: str, extra: dict | None = None) -> Path:
    """Write a minimal apm.yml and return its path."""
    data = {"name": "test-project", "version": "1.0"}
    if extra:
        data.update(extra)
    manifest = Path(tmpdir) / "apm.yml"
    manifest.write_text(yaml.dump(data))
    return manifest


def _write_local_replacement() -> None:
    """Install acme/hooky replaced by ./local-hooky (one hook) in the cwd."""
    from apm_cli.deps.lockfile import LockFile

    local_pkg = Path("local-hooky")
    (local_pkg / ".apm" / "hooks").mkdir(parents=True)
    (local_pkg / "apm.yml").write_text(yaml.dump({"name": "hooky", "version": "1.0.0"}))
    (local_pkg / ".apm" / "hooks" / "hook.json").write_text("{}")
    Path("apm_modules").mkdir()
    LockFile.from_yaml(
        """lockfile_version: '3'
replacements:
  - for:
      git: https://github.com/acme/hooky.git
    with:
      path: ./local-hooky
dependencies:
  - repo_url: acme/hooky
    version: 1.0.0
    replacement:
      source:
        path: ./local-hooky
      requests:
        - parent: null
          ref: null
          skills: null
          targets: null
          dev: false
          alias: null
"""
    ).write(Path("apm.lock.yaml"))


def _create_pkg_with_hooks(apm_modules: Path, name: str) -> None:
    """Create a package directory with a hook file."""
    pkg_dir = apm_modules / name
    hook_dir = pkg_dir / ".apm" / "hooks"
    hook_dir.mkdir(parents=True)
    (hook_dir / "pre-tool-use.json").write_text("{}")
    (pkg_dir / "apm.yml").write_text(yaml.dump({"name": name, "version": "1.0"}))


def _create_pkg_with_bin(apm_modules: Path, name: str) -> None:
    """Create a package directory with bin/ executables."""
    pkg_dir = apm_modules / name
    bin_dir = pkg_dir / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "tool").write_text("#!/bin/sh")
    (pkg_dir / "apm.yml").write_text(yaml.dump({"name": name, "version": "2.0"}))


def test_approval_scanner_rejects_symlinked_package_manifest(tmp_path: Path) -> None:
    manifest = _write_manifest(str(tmp_path))
    package_dir = tmp_path / "apm_modules" / "repo"
    package_dir.mkdir(parents=True)
    outside = tmp_path / "outside.yml"
    outside.write_text(
        yaml.safe_dump(
            {
                "name": "evil",
                "dependencies": {"lsp": [{"name": "evil", "command": "evil-lsp"}]},
            }
        ),
        encoding="utf-8",
    )
    try:
        (package_dir / "apm.yml").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    assert scan_installed_executable_packages(manifest) == []


def _write_cyclic_local_lockfile() -> None:
    """Write a corrupt local dependency ancestry cycle."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(
            repo_url="_local/a",
            source="local",
            local_path="../a",
            resolved_by="_local/b",
        )
    )
    lockfile.add_dependency(
        LockedDependency(
            repo_url="_local/b",
            source="local",
            local_path="../b",
            resolved_by="_local/a",
        )
    )
    lockfile.write(Path("apm.lock.yaml"))


def _isolated_config(tmp_path: Path):
    """Patch the user-config + legacy-approvals seams onto tmp_path."""
    cfg = tmp_path / "config.json"
    legacy = tmp_path / "approvals.yml"
    return (
        patch("apm_cli.security.executables._user_config_file", lambda: cfg),
        patch("apm_cli.security.executables._legacy_approvals_path", lambda: legacy),
        cfg,
    )


def test_approval_policy_uses_chain_aware_discovery(tmp_path: Path) -> None:
    policy = ApmPolicy(executables=ExecutablesPolicy(recommend=("hook-pkg",)))
    result = PolicyFetchResult(policy=policy, source="org:contoso/.github", outcome="found")
    with (
        patch(
            "apm_cli.policy.discovery.discover_policy",
            side_effect=AssertionError("lower-level discovery bypass"),
        ),
        patch(
            "apm_cli.policy.discovery.discover_policy_with_chain",
            return_value=result,
        ) as mock_chain,
    ):
        loaded = load_org_policy(tmp_path)
    assert loaded == policy
    mock_chain.assert_called_once_with(tmp_path)


@pytest.mark.parametrize("outcome", ["absent", "no_git_remote", "disabled", "empty"])
def test_approval_policy_benign_miss_does_not_warn(tmp_path: Path, outcome: str) -> None:
    logger = MagicMock(spec=CommandLogger)
    result = PolicyFetchResult(policy=None, outcome=outcome)

    with patch(
        "apm_cli.policy.discovery.discover_policy_with_chain",
        return_value=result,
    ):
        loaded = load_org_policy(tmp_path, logger=logger)

    assert loaded == ApmPolicy()
    logger.warning.assert_not_called()


@pytest.mark.parametrize("outcome", ["malformed", "cache_miss_fetch_fail", "garbage_response"])
def test_approval_policy_resolution_failure_is_reported_through_the_owner(
    tmp_path: Path,
    outcome: str,
) -> None:
    logger = MagicMock(spec=CommandLogger)
    result = PolicyFetchResult(policy=None, outcome=outcome, source="org:contoso/.github")

    with (
        patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=result),
        patch("apm_cli.utils.console._rich_warning") as warning,
    ):
        loaded = load_org_policy(tmp_path, logger=logger)

    assert loaded == ApmPolicy()
    warning.assert_called_once()
    assert f"outcome={outcome}" in warning.call_args.args[0]


@pytest.mark.parametrize("outcome", ["hash_mismatch", "incomplete_chain"])
def test_approval_policy_blocking_outcome_raises_without_error_detail(
    tmp_path: Path, outcome: str
) -> None:
    logger = MagicMock(spec=CommandLogger)
    result = PolicyFetchResult(
        policy=None,
        outcome=outcome,
        error="SENSITIVE_URL_TOKEN_HASH_EXCEPTION",
    )

    with (
        patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=result),
        patch("apm_cli.utils.console._rich_warning") as warning,
        pytest.raises(PolicyViolationError) as excinfo,
    ):
        load_org_policy(tmp_path, logger=logger)

    assert "SENSITIVE_URL_TOKEN_HASH_EXCEPTION" not in str(excinfo.value)
    warning.assert_not_called()


def test_approve_recommended_warns_when_org_policy_discovery_fails() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        _write_manifest(".")
        _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
        with patch(
            "apm_cli.policy.discovery.discover_policy_with_chain",
            side_effect=RuntimeError("SENSITIVE_TRANSPORT_DETAIL"),
        ):
            result = runner.invoke(approve_cmd, ["--recommended"])

    assert result.exit_code == 0
    assert "Org policy not applied (outcome=cache_miss_fetch_fail" in " ".join(
        result.output.split()
    )
    assert "SENSITIVE_TRANSPORT_DETAIL" not in result.output


def test_approve_exits_on_blocking_policy_outcome_without_writing() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        _write_manifest(".")
        _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
        before = Path("apm.yml").read_bytes()
        with patch(
            "apm_cli.policy.discovery.discover_policy_with_chain",
            return_value=PolicyFetchResult(outcome="incomplete_chain"),
        ):
            result = runner.invoke(approve_cmd, ["--recommended"])
        after = Path("apm.yml").read_bytes()

    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "Blocked by org policy" in flat
    assert "apm policy status --no-cache" in flat
    assert after == before


# ---------------------------------------------------------------------------
# _find_matching_key
# ---------------------------------------------------------------------------


class TestFindMatchingKey:
    def test_exact_match(self) -> None:
        allow = {"owner/repo#v1.0": {"hooks": True}}
        assert _find_matching_key(allow, "owner/repo#v1.0") == "owner/repo#v1.0"

    def test_prefix_match(self) -> None:
        allow = {"owner/repo#v1.0": {"hooks": True}}
        assert _find_matching_key(allow, "owner/repo") == "owner/repo#v1.0"

    def test_no_match(self) -> None:
        allow = {"other/repo#v1.0": {"hooks": True}}
        assert _find_matching_key(allow, "owner/repo") is None

    def test_empty_dict(self) -> None:
        assert _find_matching_key({}, "anything") is None


def test_port_less_name_points_at_the_ported_package() -> None:
    """A port-less name never grants a ported package but names its key (#3967)."""
    from apm_cli.commands.approve import _not_found_message
    from apm_cli.security.executables import ExecutableDeclaration

    ported = ExecutableDeclaration(package_key="github.com:8443/acme/hooky", package_name="hooky")
    other = ExecutableDeclaration(package_key="acme/other", package_name="other")
    message = _not_found_message([ported, other], "acme/hooky")
    assert "not found in installed packages" in message
    assert "'apm approve github.com:8443/acme/hooky'" in message
    assert _not_found_message([other], "acme/hooky") == (
        "acme/hooky: not found in installed packages"
    )


# ---------------------------------------------------------------------------
# approve_cmd
# ---------------------------------------------------------------------------


class TestApproveCmd:
    @pytest.mark.parametrize(
        "args",
        [
            ("--pending",),
            ("--recommended",),
            ("--all",),
            ("--list",),
            ("hook-pkg",),
        ],
    )
    def test_renders_logger_summary_on_success_paths(self, args: tuple[str, ...]) -> None:
        runner = CliRunner()
        logger = MagicMock(spec=CommandLogger)
        with runner.isolated_filesystem():
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            with (
                patch("apm_cli.commands.approve.CommandLogger", return_value=logger),
                patch("apm_cli.commands.approve._load_org_policy", return_value=ApmPolicy()),
            ):
                result = runner.invoke(approve_cmd, list(args))

        assert result.exit_code == 0
        logger.render_summary.assert_called_once_with()

    def test_no_manifest_exits_1(self) -> None:
        runner = CliRunner()
        logger = MagicMock(spec=CommandLogger)
        with runner.isolated_filesystem():
            with patch("apm_cli.commands.approve.CommandLogger", return_value=logger):
                result = runner.invoke(approve_cmd, [])

        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        logger.render_summary.assert_called_once_with()

    def test_no_args_shows_error(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            result = runner.invoke(approve_cmd, [])
            assert result.exit_code != 0
            assert "Specify at least one package" in result.output

    def test_pending_no_packages(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            result = runner.invoke(approve_cmd, ["--pending"])
            assert result.exit_code == 0
            assert "approved" in result.output.lower()

    def test_pending_with_unapproved_packages(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            result = runner.invoke(approve_cmd, ["--pending"])
            assert result.exit_code == 0
            assert "hook-pkg" in result.output

    def test_approve_all_writes_project_manifest(self) -> None:
        """apm approve --all writes to the project apm.yml executables block."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            _create_pkg_with_bin(Path("apm_modules"), "bin-pkg")

            result = runner.invoke(approve_cmd, ["--all"])
            assert result.exit_code == 0
            assert "Approved" in result.output

            from apm_cli.utils.yaml_io import load_yaml

            project_data = load_yaml(Path("apm.yml"))
            assert "executables" in project_data
            assert project_data["executables"]["allow"]

    def test_approve_specific_package_writes_project(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")

            result = runner.invoke(approve_cmd, ["hook-pkg"])
            assert result.exit_code == 0
            assert "Approved" in result.output

            from apm_cli.utils.yaml_io import load_yaml

            data = load_yaml(Path("apm.yml"))
            assert data["executables"]["allow"]

    def test_approve_user_scope_writes_config(self, tmp_path: Path) -> None:
        p_cfg, p_legacy, cfg = _isolated_config(tmp_path)
        runner = CliRunner()
        with runner.isolated_filesystem(), p_cfg, p_legacy:
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")

            result = runner.invoke(approve_cmd, ["--user", "hook-pkg"])
            assert result.exit_code == 0
            assert "Approved" in result.output
            assert cfg.is_file()

            import json

            stored = json.loads(cfg.read_text())
            assert stored["executables"]["allow"]
            # The project manifest is untouched under --user.
            from apm_cli.utils.yaml_io import load_yaml

            assert "executables" not in load_yaml(Path("apm.yml"))

    def test_approve_unknown_package(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            Path("apm_modules").mkdir()
            result = runner.invoke(approve_cmd, ["nonexistent"])
            # Approving nothing is an error, never a silent success (#4154).
            assert result.exit_code == 1
            assert "not found" in result.output

    @pytest.mark.parametrize("command", [approve_cmd, deny_cmd])
    def test_partial_name_never_matches_an_installed_package(self, command) -> None:
        """A prefix of a package name must not grant or deny that package."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")

            result = runner.invoke(command, ["hook"])

            from apm_cli.utils.yaml_io import load_yaml

            store = load_yaml(Path("apm.yml")).get("executables", {})
            assert "hook-pkg" not in str(store.get("allow", {}))
            assert not any("hook-pkg" in key for key in store.get("deny", {}))
            # approve of a name matching nothing exits 1 (#4154); deny records it.
            assert result.exit_code == (1 if command is approve_cmd else 0), result.output

    @pytest.mark.parametrize("command", [approve_cmd, deny_cmd])
    def test_name_matching_two_installed_packages_is_refused(self, command) -> None:
        """An ambiguous name refuses instead of picking one package."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            from apm_cli.deps.lockfile import LockFile

            _write_manifest(".")
            for owner in ("acme", "other"):
                pkg = Path("apm_modules") / owner / "hooky"
                (pkg / ".apm" / "hooks").mkdir(parents=True)
                (pkg / ".apm" / "hooks" / "pre-tool-use.json").write_text("{}")
                (pkg / "apm.yml").write_text(yaml.dump({"name": "hooky", "version": "1.0"}))
            LockFile.from_yaml(
                "lockfile_version: '3'\n"
                "dependencies:\n"
                "  - repo_url: acme/hooky\n    version: 1.0.0\n"
                "  - repo_url: other/hooky\n    version: 1.0.0\n"
            ).write(Path("apm.lock.yaml"))
            before = Path("apm.yml").read_text()

            result = runner.invoke(command, ["hooky"])

            assert result.exit_code == 1, result.output
            assert "matches more than one installed package" in result.output
            assert Path("apm.yml").read_text() == before

    @pytest.mark.parametrize("command", [approve_cmd, deny_cmd])
    def test_trust_command_reports_corrupt_local_dependency_ancestry(self, command) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            Path("apm_modules").mkdir()
            _write_cyclic_local_lockfile()

            result = runner.invoke(command, ["package-a"])

        assert result.exit_code == 1
        assert "invalid local dependency ancestry" in result.output
        assert "Run 'apm install'" in result.output

    def test_approve_uses_locked_identity_not_manifest_name(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            package_dir = Path("apm_modules/evil/repo")
            _create_pkg_with_hooks(Path("apm_modules/evil"), "repo")
            (package_dir / "apm.yml").write_text(
                yaml.dump({"name": "trusted-name", "version": "1.0"})
            )
            from apm_cli.deps.lockfile import LockedDependency, LockFile

            locked = LockedDependency(
                repo_url="evil/repo",
                version="1.0",
                name="trusted-name",
            )
            lockfile = LockFile()
            lockfile.add_dependency(locked)
            lockfile.write(Path("apm.lock.yaml"))

            result = runner.invoke(approve_cmd, ["trusted-name"])

            assert result.exit_code == 0, result.output
            from apm_cli.utils.yaml_io import load_yaml

            stored = load_yaml(Path("apm.yml"))["executables"]["allow"]
            assert set(stored) == {"evil/repo"}

    def test_approve_replaced_dependency_local_path_grants_source_key(self) -> None:
        """apm approve for a replaced dependency grants the selected source key."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            local_pkg = Path("local-hooky")
            local_pkg.mkdir()
            (local_pkg / "apm.yml").write_text(yaml.dump({"name": "hooky", "version": "1.0.0"}))
            hooks_dir = local_pkg / ".apm" / "hooks"
            hooks_dir.mkdir(parents=True)
            (hooks_dir / "hook.json").write_text("{}")
            Path("apm_modules").mkdir()

            from apm_cli.deps.lockfile import LockFile
            from apm_cli.utils.yaml_io import load_yaml

            lock_text = """lockfile_version: '3'
replacements:
  - for:
      git: https://github.com/acme/hooky.git
    with:
      path: ./local-hooky
dependencies:
  - repo_url: acme/hooky
    version: 1.0.0
    replacement:
      source:
        path: ./local-hooky
      requests:
        - parent: null
          ref: null
          skills: null
          targets: null
          dev: false
          alias: null
"""
            LockFile.from_yaml(lock_text).write(Path("apm.lock.yaml"))

            result = runner.invoke(approve_cmd, ["acme/hooky"])
            assert result.exit_code == 0, result.output
            stored = load_yaml(Path("apm.yml"))["executables"]["allow"]
            assert set(stored) == {"./local-hooky"}

    def test_approve_replaced_dependency_by_source_key(self) -> None:
        """apm approve <selected-source-key> also works for replaced dependencies."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            local_pkg = Path("local-hooky")
            local_pkg.mkdir()
            (local_pkg / "apm.yml").write_text(yaml.dump({"name": "hooky", "version": "1.0.0"}))
            hooks_dir = local_pkg / ".apm" / "hooks"
            hooks_dir.mkdir(parents=True)
            (hooks_dir / "hook.json").write_text("{}")
            Path("apm_modules").mkdir()

            from apm_cli.deps.lockfile import LockFile
            from apm_cli.utils.yaml_io import load_yaml

            lock_text = """lockfile_version: '3'
replacements:
  - for:
      git: https://github.com/acme/hooky.git
    with:
      path: ./local-hooky
dependencies:
  - repo_url: acme/hooky
    version: 1.0.0
    replacement:
      source:
        path: ./local-hooky
      requests:
        - parent: null
          ref: null
          skills: null
          targets: null
          dev: false
          alias: null
"""
            LockFile.from_yaml(lock_text).write(Path("apm.lock.yaml"))

            result = runner.invoke(approve_cmd, ["./local-hooky"])
            assert result.exit_code == 0, result.output
            stored = load_yaml(Path("apm.yml"))["executables"]["allow"]
            assert set(stored) == {"./local-hooky"}

    def test_deny_replaced_dependency_by_original_identity(self) -> None:
        """apm deny <original-identity> denies the selected source key."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            local_pkg = Path("local-hooky")
            local_pkg.mkdir()
            (local_pkg / "apm.yml").write_text(yaml.dump({"name": "hooky", "version": "1.0.0"}))
            hooks_dir = local_pkg / ".apm" / "hooks"
            hooks_dir.mkdir(parents=True)
            (hooks_dir / "hook.json").write_text("{}")
            Path("apm_modules").mkdir()

            from apm_cli.deps.lockfile import LockFile
            from apm_cli.utils.yaml_io import load_yaml

            lock_text = """lockfile_version: '3'
replacements:
  - for:
      git: https://github.com/acme/hooky.git
    with:
      path: ./local-hooky
dependencies:
  - repo_url: acme/hooky
    version: 1.0.0
    replacement:
      source:
        path: ./local-hooky
      requests:
        - parent: null
          ref: null
          skills: null
          targets: null
          dev: false
          alias: null
"""
            LockFile.from_yaml(lock_text).write(Path("apm.lock.yaml"))

            result = runner.invoke(deny_cmd, ["acme/hooky"])
            assert result.exit_code == 0, result.output
            stored = load_yaml(Path("apm.yml"))["executables"]["deny"]
            assert set(stored) == {"./local-hooky", "acme/hooky"}

    def test_approve_original_identity_beats_lookalike_package_name(self) -> None:
        """A package naming itself acme/hooky cannot capture apm approve acme/hooky."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            lookalike = Path("apm_modules/aaa/lookalike")
            lookalike.mkdir(parents=True)
            (lookalike / "apm.yml").write_text(
                yaml.dump({"name": "acme/hooky", "version": "9.9.9"})
            )
            (lookalike / ".apm" / "hooks").mkdir(parents=True)
            (lookalike / ".apm" / "hooks" / "hook.json").write_text("{}")
            local_pkg = Path("local-hooky")
            local_pkg.mkdir()
            (local_pkg / "apm.yml").write_text(yaml.dump({"name": "hooky", "version": "1.0.0"}))
            (local_pkg / ".apm" / "hooks").mkdir(parents=True)
            (local_pkg / ".apm" / "hooks" / "hook.json").write_text("{}")

            from apm_cli.deps.lockfile import LockFile
            from apm_cli.utils.yaml_io import load_yaml

            lock_text = """lockfile_version: '3'
replacements:
  - for:
      git: https://github.com/acme/hooky.git
    with:
      path: ./local-hooky
dependencies:
  - repo_url: aaa/lookalike
    version: 9.9.9
  - repo_url: acme/hooky
    version: 1.0.0
    replacement:
      source:
        path: ./local-hooky
      requests:
        - parent: null
          ref: null
          skills: null
          targets: null
          dev: false
          alias: null
"""
            LockFile.from_yaml(lock_text).write(Path("apm.lock.yaml"))

            result = runner.invoke(approve_cmd, ["acme/hooky"])
            assert result.exit_code == 0, result.output
            stored = load_yaml(Path("apm.yml"))["executables"]["allow"]
            assert set(stored) == {"./local-hooky"}

    def test_approve_replaced_dependency_by_versioned_original_name(self) -> None:
        """apm approve acme/hooky#1.0.0 still grants the replaced row's source key."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            _write_local_replacement()
            from apm_cli.utils.yaml_io import load_yaml

            result = runner.invoke(approve_cmd, ["acme/hooky#1.0.0"])
            assert result.exit_code == 0, result.output
            stored = load_yaml(Path("apm.yml"))["executables"]["allow"]
            assert set(stored) == {"./local-hooky"}

    def test_deny_replaced_dependency_drops_upstream_allow(self) -> None:
        """apm deny <original> removes an old upstream grant as well."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(
                ".",
                {
                    "executables": {
                        "allow": {
                            "acme/hooky": {"hooks": True},
                            "./local-hooky": {"hooks": True},
                        }
                    }
                },
            )
            _write_local_replacement()
            from apm_cli.utils.yaml_io import load_yaml

            result = runner.invoke(deny_cmd, ["acme/hooky"])
            assert result.exit_code == 0, result.output
            executables = load_yaml(Path("apm.yml"))["executables"]
            assert set(executables["deny"]) == {"./local-hooky", "acme/hooky"}
            assert not executables.get("allow")

    def test_approve_replaced_dependency_git_fork_grants_fork_key(self) -> None:
        """apm approve for a fork-replaced dependency grants the fork approval key."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            pkg = Path("apm_modules/acme/hooky")
            pkg.mkdir(parents=True)
            (pkg / "apm.yml").write_text(yaml.dump({"name": "hooky", "version": "1.2.0"}))
            hooks_dir = pkg / ".apm" / "hooks"
            hooks_dir.mkdir(parents=True)
            (hooks_dir / "hook.json").write_text("{}")

            from apm_cli.deps.lockfile import LockFile
            from apm_cli.utils.yaml_io import load_yaml

            lock_text = """lockfile_version: '3'
replacements:
  - for:
      git: https://github.com/acme/hooky.git
    with:
      git: https://github.com/myfork/hooky.git
      ref: v1.2.0
dependencies:
  - repo_url: acme/hooky
    version: 1.0.0
    replacement:
      source:
        git: https://github.com/myfork/hooky.git
        ref: v1.2.0
      requests:
        - parent: null
          ref: null
          skills: null
          targets: null
          dev: false
          alias: null
"""
            LockFile.from_yaml(lock_text).write(Path("apm.lock.yaml"))

            result = runner.invoke(approve_cmd, ["acme/hooky"])
            assert result.exit_code == 0, result.output
            stored = load_yaml(Path("apm.yml"))["executables"]["allow"]
            # Pins bind content, so the key is the version-blind selected source.
            assert set(stored) == {"myfork/hooky"}

    def test_approve_recommended_bulk_accepts_org_set(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            policy = ApmPolicy(executables=ExecutablesPolicy(recommend=("hook-pkg",)))

            with patch("apm_cli.commands.approve._load_org_policy", return_value=policy):
                result = runner.invoke(approve_cmd, ["--recommended"])

            assert result.exit_code == 0
            assert "Approved" in result.output
            from apm_cli.utils.yaml_io import load_yaml

            assert load_yaml(Path("apm.yml"))["executables"]["allow"]

    def test_approve_recommended_empty_set(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            with patch("apm_cli.commands.approve._load_org_policy", return_value=ApmPolicy()):
                result = runner.invoke(approve_cmd, ["--recommended"])
            assert result.exit_code == 0
            assert "No org-recommended" in result.output

    def test_approve_list_shows_decisions(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".", {"executables": {"allow": {}}})
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            with patch("apm_cli.commands.approve._load_org_policy", return_value=ApmPolicy()):
                result = runner.invoke(approve_cmd, ["--list"])
            assert result.exit_code == 0
            assert "hook-pkg" in result.output
            # N1 (#1873): a blocked/parked package surfaces a footer CTA.
            assert "parked" in result.output
            assert "--recommended" in result.output


# ---------------------------------------------------------------------------
# deny_cmd
# ---------------------------------------------------------------------------


class TestDenyCmd:
    def test_deny_writes_project_deny(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            result = runner.invoke(deny_cmd, ["hook-pkg"])
            assert result.exit_code == 0
            assert "Denied" in result.output

            from apm_cli.utils.yaml_io import load_yaml

            data = load_yaml(Path("apm.yml"))
            assert data["executables"]["deny"]

    def test_deny_uninstalled_package(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            Path("apm_modules").mkdir()
            result = runner.invoke(deny_cmd, ["owner/repo"])
            assert result.exit_code == 0
            assert "Denied" in result.output
            from apm_cli.security.executables import ALL_EXEC_TYPES
            from apm_cli.utils.yaml_io import load_yaml

            stored = load_yaml(Path("apm.yml"))["executables"]["deny"]["owner/repo"]
            assert set(stored) == set(ALL_EXEC_TYPES)
            assert all(stored.values())

    def test_deny_user_scope_writes_config(self, tmp_path: Path) -> None:
        p_cfg, p_legacy, cfg = _isolated_config(tmp_path)
        runner = CliRunner()
        with runner.isolated_filesystem(), p_cfg, p_legacy:
            _write_manifest(".")
            Path("apm_modules").mkdir()
            result = runner.invoke(deny_cmd, ["--user", "owner/repo"])
            assert result.exit_code == 0
            import json

            stored = json.loads(cfg.read_text())
            assert stored["executables"]["deny"]


# ---------------------------------------------------------------------------
# apm policy explain
# ---------------------------------------------------------------------------


class TestExplainCmd:
    def test_explain_unknown_package(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            Path("apm_modules").mkdir()
            with patch("apm_cli.commands.approve._load_org_policy", return_value=ApmPolicy()):
                result = runner.invoke(policy_group, ["explain", "nonexistent"])
            assert result.exit_code == 0
            assert "not found" in result.output

    def test_explain_blocked_package_shows_layer_and_remedy(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            # Gate enabled (executables block present) but nothing approved.
            _write_manifest(".", {"executables": {"allow": {}}})
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            with patch("apm_cli.commands.approve._load_org_policy", return_value=ApmPolicy()):
                result = runner.invoke(policy_group, ["explain", "hook-pkg"])
            assert result.exit_code == 0
            assert "blocked" in result.output
            assert "default-deny" in result.output
            assert "apm approve" in result.output

    def test_explain_with_gate_disabled_does_not_promise_deployment(self) -> None:
        """A disabled executables gate is not an org-policy verdict (#3994)."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            with patch("apm_cli.commands.approve._load_org_policy", return_value=ApmPolicy()):
                result = runner.invoke(policy_group, ["explain", "hook-pkg"])
            assert result.exit_code == 0
            output = " ".join(result.output.split())
            assert "all executables deploy" not in output
            assert "Org policy can still block the package" in output

    def test_explain_allowed_via_project(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            _write_manifest(
                ".",
                {"executables": {"allow": {"hook-pkg": {"hooks": True}}}},
            )
            with patch("apm_cli.commands.approve._load_org_policy", return_value=ApmPolicy()):
                result = runner.invoke(policy_group, ["explain", "hook-pkg"])
            assert result.exit_code == 0
            assert "allowed" in result.output
            assert "project-allow" in result.output


def test_approve_clears_a_deny_stored_under_another_spelling() -> None:
    """A URL-form deny blocks ``acme/hooky``, so approving it must clear that deny."""
    from apm_cli.commands.approve import _same_store_denies
    from apm_cli.security.executables import ExecutableDeclaration

    decl = ExecutableDeclaration(package_key="acme/hooky#1.0.0", package_name="hooky")
    deny = {
        "https://github.com/acme/hooky.git": {"hooks": True},
        "Acme/Hooky": {"bin": True},
        "acme/other": {"hooks": True},
    }

    assert sorted(_same_store_denies(deny, decl)) == [
        "Acme/Hooky",
        "https://github.com/acme/hooky.git",
    ]


def test_bulk_approval_skips_an_org_denied_package_and_approves_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``apm approve --all`` writes no allow for a package another layer denies (#3957)."""
    project = tmp_path / "project"
    apm_home = tmp_path / "home" / ".apm"
    apm_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(apm_home.parent))
    monkeypatch.setenv("APM_HOME", str(apm_home))
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(apm_home / "config.json"))
    for name in ("org-denied", "fine"):
        hooks = project / "apm_modules" / name / ".apm" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "guard.json").write_text("{}", encoding="utf-8")
        (project / "apm_modules" / name / "apm.yml").write_text(
            f"name: {name}\nversion: '1.0'\n", encoding="utf-8"
        )
    manifest = project / "apm.yml"
    manifest.write_text("name: consumer\nversion: '1.0'\n", encoding="utf-8")
    monkeypatch.chdir(project)
    policy = ApmPolicy(executables=ExecutablesPolicy(deny=["org-denied"]))

    with patch("apm_cli.commands.approve._load_org_policy", return_value=policy):
        result = CliRunner().invoke(approve_cmd, ["--all"])

    allow = (yaml.safe_load(manifest.read_text(encoding="utf-8")).get("executables") or {}).get(
        "allow"
    ) or {}
    assert not any("org-denied" in key for key in allow), result.output
    assert any("fine" in key for key in allow), result.output
    assert "Skipped org-denied" in " ".join(result.output.split())
