"""Integration guardrails for install and policy outcome authorities."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _write_plugin_consumer(tmp_path: Path, plugin_manifest: dict) -> tuple[Path, Path]:
    """Create a local plugin and a consumer that installs it."""
    import json

    plugin = tmp_path / "plugin"
    manifest_dir = plugin / ".claude-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(plugin_manifest),
        encoding="utf-8",
    )
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / ".claude").mkdir()
    (consumer / "apm.yml").write_text(
        "name: consumer\n"
        "version: 1.0.0\n"
        "targets: [claude]\n"
        "dependencies:\n"
        "  apm:\n"
        "    - path: ../plugin\n",
        encoding="utf-8",
    )
    return plugin, consumer


def test_install_result_disposition_owns_cli_exit_code() -> None:
    """Service classification and adapter exit translation must agree."""
    from apm_cli.install.outcome import finalize_install_result
    from apm_cli.models.results import InstallDisposition, InstallResult

    diagnostics = MagicMock(error_count=1, has_critical_security=False)
    result = finalize_install_result(
        InstallResult(diagnostics=diagnostics),
        force=False,
    )

    assert result.disposition is InstallDisposition.FAILED
    assert result.exit_code == 1


def test_missing_declared_plugin_component_fails_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit missing plugin path is an unsatisfied install requirement."""
    from click.testing import CliRunner

    from apm_cli.cli import cli

    plugin, consumer = _write_plugin_consumer(
        tmp_path,
        {
            "name": "missing-components",
            "version": "1.0.0",
            "agents": ["./agents/does-not-exist.agent.md"],
            "skills": ["./skills/does-not-exist"],
        },
    )
    monkeypatch.chdir(consumer)
    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)

    result = CliRunner().invoke(cli, ["install"])

    assert result.exit_code != 0, result.output
    assert "missing-components" in result.output
    assert "agents" in result.output
    assert "./agents/does-not-exist.agent.md" in result.output
    assert "plugin root" in result.output
    assert "remove the declaration" in result.output
    assert not (consumer / "apm.lock.yaml").exists()
    assert not (consumer / ".claude" / "agents").exists()
    assert plugin.is_dir()


def test_requested_plugin_skill_with_no_match_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A named --skill miss must not become a successful no-op."""
    from click.testing import CliRunner

    from apm_cli.cli import cli

    plugin, consumer = _write_plugin_consumer(
        tmp_path,
        {
            "name": "selective-skills",
            "version": "1.0.0",
            "skills": ["./skills/engineering/tdd"],
        },
    )
    for name in ("tdd", "resolving-merge-conflicts"):
        skill = plugin / "skills" / "engineering" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    monkeypatch.chdir(consumer)
    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)

    result = CliRunner().invoke(
        cli,
        ["install", "--skill", "engineering/resolving-merge-conflicts"],
    )

    assert result.exit_code != 0, result.output
    assert "engineering/resolving-merge-conflicts" in result.output
    assert "matched no declared skills" in result.output
    assert "tdd" in result.output
    assert "update the package manifest" in result.output
    assert "then reinstall" in result.output
    assert not (consumer / "apm.lock.yaml").exists()
    assert not (consumer / ".claude" / "skills" / "resolving-merge-conflicts").exists()


def test_stale_persisted_skill_pin_warns_instead_of_silent_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted skill pin that deploys nothing must remain visible."""
    from click.testing import CliRunner

    from apm_cli.cli import cli

    bundle = tmp_path / "bundle"
    skill = bundle / "skills" / "tdd"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# tdd\n", encoding="utf-8")
    (bundle / "apm.yml").write_text(
        "name: skill-bundle\nversion: 1.0.0\ndescription: local skill bundle\n",
        encoding="utf-8",
    )
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / ".github").mkdir()
    (consumer / "apm.yml").write_text(
        "name: consumer\n"
        "version: 1.0.0\n"
        "targets: [copilot]\n"
        "dependencies:\n"
        "  apm:\n"
        "    - path: ../bundle\n"
        "      skills: [missing]\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(consumer)
    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)

    result = CliRunner().invoke(cli, ["install"])

    assert result.exit_code == 0, result.output
    assert "Skill selection matched no available skills" in result.output
    assert "Requested:" in result.output
    assert "missing" in result.output
    assert "Available:" in result.output
    assert "tdd" in result.output
    normalized_output = " ".join(result.output.split())
    assert (
        "Edit 'skills:' in apm.yml to use an available name or remove the filter"
        in normalized_output
    )
    assert not (consumer / ".github" / "skills" / "missing").exists()


def test_pipeline_diagnostics_make_install_exit_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real CLI adapter must use the service-owned disposition."""
    from click.testing import CliRunner

    from apm_cli.cli import cli
    from apm_cli.utils.diagnostics import DiagnosticCollector

    (tmp_path / "apm.yml").write_text(
        "name: demo\nversion: 1.0.0\ntargets: [copilot]\n",
        encoding="utf-8",
    )
    (tmp_path / ".github").mkdir()
    diagnostics = DiagnosticCollector()
    diagnostics.error("integration failed")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)
    monkeypatch.setattr(
        "apm_cli.commands.install._install_apm_packages",
        lambda *_a, **_k: (0, 0, 0, diagnostics),
    )

    result = CliRunner().invoke(cli, ["install"])

    assert result.exit_code == 1
    assert "Installation failed" in result.output
    assert "Install interrupted" not in result.output


def test_handled_install_error_uses_failure_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handled errors must not be mislabeled as interruptions."""
    from click.testing import CliRunner

    from apm_cli.cli import cli

    (tmp_path / "apm.yml").write_text("name: demo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)

    result = CliRunner().invoke(cli, ["install"])

    assert result.exit_code == 1
    assert "Install failed after" in result.output
    assert "Install interrupted" not in result.output


def test_failed_install_does_not_fire_post_install_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service lifecycle hooks must observe the classified result."""
    from apm_cli.install.request import InstallRequest
    from apm_cli.install.service import InstallService
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.models.results import InstallDisposition, InstallResult

    runner = MagicMock()
    monkeypatch.setattr(
        InstallService,
        "_build_script_runner",
        staticmethod(lambda _request: runner),
    )
    monkeypatch.setattr(
        "apm_cli.install.pipeline.run_install_pipeline",
        lambda *_a, **_k: InstallResult(
            disposition=InstallDisposition.FAILED,
            exit_code=1,
        ),
    )

    result = InstallService().run(
        InstallRequest(apm_package=APMPackage(name="demo", version="1.0.0"))
    )

    assert result.disposition is InstallDisposition.FAILED
    assert [call.args[0] for call in runner.fire.call_args_list] == ["pre-install"]


def test_cancelled_install_skips_mcp_and_lsp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declining a plan must terminate all downstream mutation phases."""
    from click.testing import CliRunner

    from apm_cli.cli import cli
    from apm_cli.models.results import InstallDisposition, InstallResult

    (tmp_path / "apm.yml").write_text(
        "name: demo\nversion: 1.0.0\ntargets: [copilot]\ndependencies:\n  apm:\n    - owner/repo\n",
        encoding="utf-8",
    )
    (tmp_path / ".github").mkdir()
    mcp_install = MagicMock()
    lsp_install = MagicMock()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)
    monkeypatch.setattr(
        "apm_cli.commands.install._install_apm_dependencies",
        lambda *_a, **_k: InstallResult(disposition=InstallDisposition.CANCELLED),
    )
    monkeypatch.setattr("apm_cli.install.mcp.run_mcp_integration", mcp_install)
    monkeypatch.setattr("apm_cli.install.lsp.run_lsp_integration", lsp_install)

    result = CliRunner().invoke(cli, ["install"])

    assert result.exit_code == 0, result.output
    mcp_install.assert_not_called()
    lsp_install.assert_not_called()


def test_manifest_inheritance_cannot_relax_explicit_includes() -> None:
    """Either ancestor or child may tighten explicit-include enforcement."""
    from apm_cli.policy.inheritance import merge_policies
    from apm_cli.policy.schema import ApmPolicy, ManifestPolicy

    parent_true = ApmPolicy(manifest=ManifestPolicy(require_explicit_includes=True))
    child_false = ApmPolicy(manifest=ManifestPolicy(require_explicit_includes=False))
    parent_false = ApmPolicy(manifest=ManifestPolicy(require_explicit_includes=False))
    child_true = ApmPolicy(manifest=ManifestPolicy(require_explicit_includes=True))

    assert merge_policies(parent_true, child_false).manifest.require_explicit_includes
    assert merge_policies(parent_false, child_true).manifest.require_explicit_includes


def test_explicit_policy_uses_chain_aware_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit leaves must resolve ancestors through the shared entry point."""
    from apm_cli.policy import discovery
    from apm_cli.policy.schema import ApmPolicy

    calls: list[str | None] = []
    leaf = discovery.PolicyFetchResult(
        policy=ApmPolicy(extends="owner/parent"),
        source="org:owner/leaf",
        outcome="found",
    )
    missing_parent = discovery.PolicyFetchResult(
        policy=None,
        source="org:owner/parent",
        outcome="cache_miss_fetch_fail",
        error="unreachable",
    )

    def fake_discover(_root, *, policy_override=None, **_kwargs):
        calls.append(policy_override)
        return leaf if len(calls) == 1 else missing_parent

    monkeypatch.setattr(discovery, "discover_policy", fake_discover)

    result = discovery.discover_policy_with_chain(
        tmp_path,
        policy_override="owner/leaf",
        no_cache=True,
    )

    assert calls == ["owner/leaf", "owner/parent"]
    assert result.outcome == "incomplete_chain"
    assert result.policy is None


def test_incomplete_policy_chain_always_fails_closed() -> None:
    """A partial ancestor set must never become an enforceable policy."""
    from apm_cli.install.errors import PolicyViolationError
    from apm_cli.policy.discovery import PolicyFetchResult
    from apm_cli.policy.outcome_routing import route_discovery_outcome

    result = PolicyFetchResult(
        policy=None,
        source="org:owner/leaf",
        outcome="incomplete_chain",
        error="parent unreachable",
    )

    with pytest.raises(PolicyViolationError):
        route_discovery_outcome(
            result,
            logger=MagicMock(),
            fetch_failure_default="warn",
        )
