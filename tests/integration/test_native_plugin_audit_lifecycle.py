"""End-to-end audit and drift lifecycle for native Copilot Agent Plugins (issue #3214).

Proves that:
1. A fixture containing a native Copilot plugin and a skill, both with and without
   an accompanying apm.yml, installs successfully and passes `apm audit --ci`.
2. Reinstall followed by audit remains idempotent and clean.
3. Audit is strictly read-only: project files, lockfile, and package sources remain
   byte-for-byte identical.
4. Modified managed content produces meaningful drift and content-integrity failures.
5. Missing managed content produces meaningful unintegrated and deployed-file failures.
6. Tampered or removed plugin manifest produces meaningful drift replay failures.
7. Target exclusion is preserved when non-Copilot targets are specified.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from tests.unit.copilot_plugins._builders import write_agent_plugin

pytestmark = [pytest.mark.integration]


def _write_project_with_skill(
    project: Path,
    dependencies: list[str],
    *,
    skill_name: str = "local-skill",
    targets: list[str] | None = None,
) -> None:
    """Create a consumer project with dependencies and an authored skill."""
    project.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "name": "consumer-app",
        "version": "1.0.0",
        "dependencies": {"apm": dependencies},
    }
    if targets is not None:
        manifest["targets"] = targets
    (project / "apm.yml").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    skill_dir = project / ".apm" / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: Authored project skill\n---\n\nProject skill body.\n",
        encoding="utf-8",
    )


def _run_install(
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    *args: str,
):
    monkeypatch.chdir(project)
    return CliRunner().invoke(
        cli,
        ["install", "--no-policy", *args],
        catch_exceptions=False,
    )


def _run_audit(
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    *args: str,
):
    monkeypatch.chdir(project)
    return CliRunner().invoke(
        cli,
        ["audit", *args],
        catch_exceptions=False,
    )


def _snapshot_tree(root: Path) -> dict[str, tuple[int, bytes]]:
    """Snapshot every file under ``root`` as ``{relpath: (size, sha256)}``."""
    snap: dict[str, tuple[int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        snap[rel] = (len(data), hashlib.sha256(data).digest())
    return snap


def test_native_plugin_without_apm_yml_and_skill_audit_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixture with native plugin (no apm.yml) and a skill installs and audits cleanly."""
    project = tmp_path / "project"
    source = tmp_path / "plugins" / "hello"
    write_agent_plugin(source, name="hello", with_mcp=False)

    _write_project_with_skill(project, [str(source)], targets=["copilot"])

    # 1. Clean install
    install_res = _run_install(monkeypatch, project, "--target", "copilot")
    assert install_res.exit_code == 0, install_res.output

    # 2. Audit --ci must pass with all checks clean (including drift and config-consistency)
    audit_res = _run_audit(monkeypatch, project, "--ci")
    assert audit_res.exit_code == 0, audit_res.output
    assert "All 10 check(s) passed" in audit_res.output
    assert "no drift detected against lockfile" in audit_res.output
    assert "No MCP configs to check" in audit_res.output

    # 3. Idempotent reinstall followed by audit remains clean
    reinstall_res = _run_install(monkeypatch, project, "--target", "copilot")
    assert reinstall_res.exit_code == 0, reinstall_res.output

    reaudit_res = _run_audit(monkeypatch, project, "--ci")
    assert reaudit_res.exit_code == 0, reaudit_res.output
    assert "All 10 check(s) passed" in reaudit_res.output
    assert "no drift detected against lockfile" in reaudit_res.output


def test_native_plugin_with_accompanying_apm_yml_and_skill_audit_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixture with native plugin (with accompanying apm.yml) and a skill installs and audits cleanly."""
    project = tmp_path / "project"
    source = tmp_path / "plugins" / "hello_with_yml"
    write_agent_plugin(source, name="hello-with-yml", with_mcp=False)
    # Add non-conflicting metadata apm.yml alongside plugin.json
    (source / "apm.yml").write_text(
        json.dumps(
            {
                "name": "hello-with-yml",
                "version": "1.0.0",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    _write_project_with_skill(project, [str(source)], targets=["copilot"])

    install_res = _run_install(monkeypatch, project, "--target", "copilot")
    assert install_res.exit_code == 0, install_res.output

    audit_res = _run_audit(monkeypatch, project, "--ci")
    assert audit_res.exit_code == 0, audit_res.output
    assert "All 10 check(s) passed" in audit_res.output
    assert "no drift detected against lockfile" in audit_res.output

    # Reinstall and re-audit
    reinstall_res = _run_install(monkeypatch, project, "--target", "copilot")
    assert reinstall_res.exit_code == 0, reinstall_res.output

    reaudit_res = _run_audit(monkeypatch, project, "--ci")
    assert reaudit_res.exit_code == 0, reaudit_res.output
    assert "All 10 check(s) passed" in reaudit_res.output
    assert "no drift detected against lockfile" in reaudit_res.output


def test_audit_strictly_preserves_project_and_lockfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit is strictly read-only and leaves project, lockfile, and sources untouched."""
    project = tmp_path / "project"
    source = tmp_path / "plugins" / "immutable_plugin"
    write_agent_plugin(source, name="immutable-plugin", with_mcp=True)

    _write_project_with_skill(project, [str(source)], targets=["copilot"])

    install_res = _run_install(monkeypatch, project, "--target", "copilot")
    assert install_res.exit_code == 0, install_res.output

    before_project_snap = _snapshot_tree(project)
    before_source_snap = _snapshot_tree(source)

    audit_res = _run_audit(monkeypatch, project, "--ci")
    assert audit_res.exit_code == 0, audit_res.output

    after_project_snap = _snapshot_tree(project)
    after_source_snap = _snapshot_tree(source)

    assert before_project_snap == after_project_snap
    assert before_source_snap == after_source_snap


def test_modified_managed_content_fails_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Modifying managed files in the deployed tree produces meaningful audit failure."""
    project = tmp_path / "project"
    source = tmp_path / "plugins" / "tamper_target"
    write_agent_plugin(source, name="tamper-target", with_mcp=False)

    _write_project_with_skill(project, [str(source)], targets=["copilot"])

    install_res = _run_install(monkeypatch, project, "--target", "copilot")
    assert install_res.exit_code == 0, install_res.output

    deployed_skill = project / ".agents" / "skills" / "local-skill" / "SKILL.md"
    assert deployed_skill.is_file()
    deployed_skill.write_text("TAMPERED SKILL CONTENT", encoding="utf-8")

    audit_res = _run_audit(monkeypatch, project, "--ci")
    assert audit_res.exit_code != 0, "Audit must fail when managed content is modified"
    assert "modified: .agents/skills/local-skill/SKILL.md" in audit_res.output
    assert "hash drift" in audit_res.output or "Drift detected" in audit_res.output


def test_missing_managed_content_fails_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting managed files from the deployed tree causes audit to fail meaningfully."""
    project = tmp_path / "project"
    source = tmp_path / "plugins" / "missing_target"
    write_agent_plugin(source, name="missing-target", with_mcp=False)

    _write_project_with_skill(project, [str(source)], targets=["copilot"])

    install_res = _run_install(monkeypatch, project, "--target", "copilot")
    assert install_res.exit_code == 0, install_res.output

    deployed_skill = project / ".agents" / "skills" / "local-skill" / "SKILL.md"
    assert deployed_skill.is_file()
    deployed_skill.unlink()

    audit_res = _run_audit(monkeypatch, project, "--ci")
    assert audit_res.exit_code != 0, "Audit must fail when deployed content is missing"
    assert (
        "unintegrated: .agents/skills/local-skill/SKILL.md" in audit_res.output
        or "deployed file missing" in audit_res.output
    )


def test_tampered_plugin_manifest_fails_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tampering with plugin.json produces a meaningful audit failure."""
    project = tmp_path / "project"
    source = tmp_path / "plugins" / "tampered_plugin"
    write_agent_plugin(source, name="tampered-plugin", with_mcp=False)

    _write_project_with_skill(project, [str(source)], targets=["copilot"])

    install_res = _run_install(monkeypatch, project, "--target", "copilot")
    assert install_res.exit_code == 0, install_res.output

    # Tamper with plugin.json
    (source / "plugin.json").write_text('{"name": "tampered", "version": "bad"}', encoding="utf-8")

    audit_res = _run_audit(monkeypatch, project, "--ci")
    assert audit_res.exit_code != 0, "Audit must fail when plugin manifest is tampered"
    assert "drift replay blocked" in audit_res.output


def test_target_exclusion_preserves_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evaluating non-copilot targets preserves exclusion without replay crash."""
    project = tmp_path / "project"
    source = tmp_path / "plugins" / "copilot_only"
    write_agent_plugin(source, name="copilot-only", with_mcp=False)

    _write_project_with_skill(project, [str(source)], targets=["copilot"])

    install_res = _run_install(monkeypatch, project, "--target", "copilot")
    assert install_res.exit_code == 0, install_res.output

    # Auditing explicitly with --target claude (which excludes copilot)
    audit_res = _run_audit(monkeypatch, project, "--ci", "--target", "claude")
    # Drift check succeeds without crashing because target exclusion is handled cleanly
    assert "drift replay blocked" not in audit_res.output
