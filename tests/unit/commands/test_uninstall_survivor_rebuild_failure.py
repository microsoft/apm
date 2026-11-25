"""A survivor rebuild that raises must fail the uninstall (#4954)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.uninstall.cli import uninstall
from apm_cli.commands.uninstall.engine import _sync_integrations_after_uninstall
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.utils.yaml_io import dump_yaml

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-post-uninstall-dependency-reachability"),
]


def _survivor_plan() -> list[tuple[MagicMock, object]]:
    dep_ref = MagicMock()
    dep_ref.get_unique_key.return_value = "acme/survivor"
    dep_ref.get_identity.return_value = "acme/survivor"
    dep_ref.get_display_name.return_value = "acme/survivor"
    pkg_info = MagicMock()
    return [(dep_ref, pkg_info)]


def test_survivor_rebuild_exception_is_recorded_as_a_package_failure(
    tmp_path: Path,
) -> None:
    """The caught exception counts as a failed package, so uninstall is incomplete."""
    apm_package = MagicMock()
    apm_package.canonical_targets = []
    apm_package.get_all_apm_dependencies.return_value = []
    logger = MagicMock()

    with (
        patch(
            "apm_cli.install.services.integrate_package_primitives",
            side_effect=RuntimeError("rebuild blew up"),
        ),
        patch(
            "apm_cli.install.target_filter.resolve_effective_package_targets",
            return_value=SimpleNamespace(targets=[]),
        ),
        patch(
            "apm_cli.install.package_resolution.effective_dependency_selection",
            return_value=SimpleNamespace(target_subset=None, skill_subset=None, skill_pairing=None),
        ),
        patch("apm_cli.security.executables.resolve_project_exec_allow_map"),
        patch("apm_cli.integration.hook_reintegration.preflight_survivor_hooks"),
    ):
        outcome = _sync_integrations_after_uninstall(
            apm_package,
            tmp_path,
            set(),
            logger,
            survivor_plan=_survivor_plan(),
            evidence=None,
        )

    assert outcome.package_failures == 1


def test_a_rebuild_that_records_an_error_before_raising_fails_the_package_once(
    tmp_path: Path,
) -> None:
    """An error the rebuild recorded, then its exception, count one failed package."""
    apm_package = MagicMock()
    apm_package.canonical_targets = []
    apm_package.get_all_apm_dependencies.return_value = []

    def record_then_raise(*_args, diagnostics, **_kwargs):
        diagnostics.error("partial write failed", package="acme/survivor")
        raise RuntimeError("rebuild blew up")

    with (
        patch(
            "apm_cli.install.services.integrate_package_primitives",
            side_effect=record_then_raise,
        ),
        patch(
            "apm_cli.install.target_filter.resolve_effective_package_targets",
            return_value=SimpleNamespace(targets=[]),
        ),
        patch(
            "apm_cli.install.package_resolution.effective_dependency_selection",
            return_value=SimpleNamespace(target_subset=None, skill_subset=None, skill_pairing=None),
        ),
        patch("apm_cli.security.executables.resolve_project_exec_allow_map"),
        patch("apm_cli.integration.hook_reintegration.preflight_survivor_hooks"),
    ):
        outcome = _sync_integrations_after_uninstall(
            apm_package,
            tmp_path,
            set(),
            MagicMock(),
            survivor_plan=_survivor_plan(),
            evidence=None,
        )

    assert outcome.package_failures == 1


def test_uninstall_command_fails_when_survivor_rebuild_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command names the failed survivor, exits non-zero and never says complete."""
    removed, survivor = "owner/removed", "owner/survivor"
    dump_yaml(
        {"name": "p", "version": "1.0.0", "dependencies": {"apm": [removed, survivor]}},
        tmp_path / "apm.yml",
    )
    LockFile(
        dependencies={key: LockedDependency(repo_url=key) for key in (removed, survivor)}
    ).write(tmp_path / "apm.lock.yaml")
    for key in (removed, survivor):
        (tmp_path / "apm_modules" / key).mkdir(parents=True)
        (tmp_path / "apm_modules" / key / "apm.yml").write_text(
            f"name: {key.split('/')[1]}\nversion: 1.0.0\n", encoding="utf-8"
        )
    prompts = tmp_path / "apm_modules" / survivor / ".apm" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "hello.prompt.md").write_text("hello", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with patch(
        "apm_cli.install.services.integrate_package_primitives",
        side_effect=RuntimeError("rebuild blew up"),
    ):
        result = CliRunner().invoke(uninstall, [removed])

    assert result.exit_code != 0, result.output
    assert survivor in result.output
    assert "Could not rebuild the surviving package's integrated files" in result.output
    assert "Uninstall incomplete: 1 package failed" in result.output
    assert "Uninstall complete" not in result.output
