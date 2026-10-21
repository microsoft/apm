"""Installed-CLI target identity and read-only audit regressions for #2816."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _new_scenario,
    _skill,
)
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]


def test_installed_legacy_mixed_targets_preserve_audit_discovery(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _new_scenario(tmp_path / "scenario", apm_binary_path)
    source = scenario.sources.create("example-package")
    content = _skill("example")
    scenario.sources.add_skill(source, "example", content)
    project = scenario.consumers.create("consumer", dependencies=({"path": str(source.root)},))
    installed = scenario.runner.run(
        (*_INSTALL_ARGS, "--target", "claude,copilot"),
        cwd=project.root,
        env=scenario.environment,
    )
    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    claude = project.root / ".claude/skills/example/SKILL.md"
    copilot = project.root / ".agents/skills/example/SKILL.md"
    assert claude.read_bytes() == copilot.read_bytes() == content.encode("ascii")
    lock_path = project.root / "apm.lock.yaml"
    legacy_lock = load_yaml(lock_path)
    legacy_lock.pop("deployments")
    dump_yaml(legacy_lock, lock_path)
    manual_path = ".github/agents/manual.agent.md"
    manual = project.root / manual_path
    manual.parent.mkdir(parents=True, exist_ok=True)
    manual.write_text("# Manual agent\nReview changes.\n", encoding="ascii")

    for damaged in (False, True):
        if damaged:
            copilot.unlink()
        before = ArtifactSnapshot.capture(tmp_path)
        audited = scenario.runner.run(
            ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json"),
            cwd=project.root,
            env=scenario.environment,
        )
        assert audited.returncode == int(damaged), (audited.stdout, audited.stderr)
        payload = json.loads(audited.stdout)
        coverage = next(
            check["primitive_coverage"]
            for check in payload["checks"]
            if check["name"] == "content-integrity"
        )
        assert any(
            row["file"] == manual_path and row["target"] == "copilot" and not row["tracked"]
            for row in coverage
        )
        assert bool(payload["drift"]["drift"]) is damaged
        if damaged:
            assert any(
                row["path"] == ".agents/skills/example/SKILL.md"
                for row in payload["drift"]["drift"]
            )
        assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))


@pytest.mark.parametrize(
    ("target", "workflow", "configured"),
    [
        ("grok-cloud", True, False),
        ("grok-cloud", True, True),
        ("grok-cloud", False, False),
        ("copilot", True, False),
    ],
)
def test_installed_target_survives_audit_and_reinstall(
    tmp_path: Path,
    apm_binary_path: Path,
    target: str,
    workflow: bool,
    configured: bool,
) -> None:
    scenario = _new_scenario(tmp_path / "scenario", apm_binary_path)
    source = scenario.sources.create("example-package")
    content = _skill("example")
    source_file = scenario.sources.add_skill(source, "example", content)
    project = scenario.consumers.create("consumer", dependencies=({"path": str(source.root)},))
    if workflow:
        signal = project.root / ".github/workflows/unrelated.yml"
        signal.parent.mkdir(parents=True)
        signal.write_text("name: unrelated\n", encoding="ascii")

    def run(*args: str, expected: int = 0) -> CommandResult:
        result = scenario.runner.run(args, cwd=project.root, env=scenario.environment)
        assert result.returncode == expected, (result.stdout, result.stderr)
        return result

    def audit(*, expected: int = 0, ci: bool = True) -> dict:
        before = ArtifactSnapshot.capture(tmp_path)
        args = ("audit", "--format", "json")
        if ci:
            args += ("--ci", "--no-policy", "--no-fail-fast")
        result = run(*args, expected=expected)
        assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))
        return json.loads(result.stdout)

    run("experimental", "enable", "grok-cloud")
    if configured:
        run("config", "set", "target", target)
    install = (*_INSTALL_ARGS, "--target", target)
    expected_path = (
        ".grok/skills/example/SKILL.md"
        if target == "grok-cloud"
        else ".agents/skills/example/SKILL.md"
    )
    unexpected_path = (
        ".agents/skills/example/SKILL.md"
        if target == "grok-cloud"
        else ".grok/skills/example/SKILL.md"
    )
    managed = project.root / expected_path
    expected_bytes = content.encode("ascii")
    expected_hash = "sha256:" + hashlib.sha256(expected_bytes).hexdigest()

    for _ in range(2):
        run(*install)
        assert managed.read_bytes() == source_file.read_bytes() == expected_bytes
        assert not (project.root / unexpected_path).exists()
        lock = load_yaml(project.root / "apm.lock.yaml")
        assert lock["dependencies"][0]["deployed_file_hashes"][expected_path] == expected_hash
        record = next(row for row in lock["deployments"] if row["value"] == expected_path)
        assert record["target"] == target
        assert record["owners"] == [str(source.root)]
        assert record["active_owner"] == str(source.root)
        assert record["content_hash"] == expected_hash
        payload = audit()
        assert payload["passed"] is True
        assert payload["drift"]["drift"] == []
        coverage = next(
            check["primitive_coverage"]
            for check in payload["checks"]
            if check["name"] == "content-integrity"
        )
        assert {
            (entry["file"], entry["target"], entry["tracked"], entry["status"])
            for entry in coverage
        } == {(expected_path, target, True, "checked")}

    bare = audit(ci=False)
    assert {(entry["file"], entry["target"]) for entry in bare["coverage"]["primitives"]} == {
        (expected_path, target)
    }

    for damage in ("missing", "modified"):
        if damage == "missing":
            managed.unlink()
        else:
            managed.write_text("# changed\n", encoding="ascii")
        payload = audit(expected=1)
        failed = {check["name"] for check in payload["checks"] if not check["passed"]}
        assert "drift" in failed
        assert ("deployed-files-present" if damage == "missing" else "content-integrity") in failed
        assert any(row["path"] == expected_path for row in payload["drift"]["drift"])
        run(*install)
        assert managed.read_bytes() == expected_bytes
        assert audit()["passed"] is True

    # A cold-cache replay must keep the same target identity, not redetect scratch.
    (project.root / "apm_modules").rename(tmp_path / "parked-modules")
    assert audit()["passed"] is True
    assert not (project.root / "apm_modules").exists()


def test_audit_preserves_current_intent_and_experimental_boundary(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _new_scenario(tmp_path / "scenario", apm_binary_path)
    source = scenario.sources.create("example-package")
    scenario.sources.add_skill(source, "example", _skill("example"))
    project = scenario.consumers.create("consumer", dependencies=({"path": str(source.root)},))

    def run(*args: str, expected: int = 0) -> CommandResult:
        before = ArtifactSnapshot.capture(tmp_path)
        result = scenario.runner.run(args, cwd=project.root, env=scenario.environment)
        assert result.returncode == expected, (result.stdout, result.stderr)
        if args[0] == "audit":
            assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))
        return result

    audit_args = ("audit", "--ci", "--no-policy", "--format", "json")
    run("experimental", "enable", "grok-cloud")
    # An install default is not retroactive authority over an explicit install.
    run("config", "set", "target", "copilot")
    run(*_INSTALL_ARGS, "--target", "grok-cloud")
    assert json.loads(run(*audit_args).stdout)["passed"] is True
    run("config", "set", "target", "claude")
    assert json.loads(run(*audit_args).stdout)["passed"] is True

    run("experimental", "disable", "grok-cloud")
    for args in (audit_args, ("audit", "--format", "json")):
        disabled = run(*args, expected=3)
        assert "grok-cloud" in disabled.stderr
        assert "apm experimental enable grok-cloud" in disabled.stderr
    assert not (project.root / ".agents").exists()
    run("experimental", "enable", "grok-cloud")
    assert json.loads(run(*audit_args).stdout)["passed"] is True

    manifest = load_yaml(project.manifest_path)
    manifest["targets"] = ["copilot"]
    dump_yaml(manifest, project.manifest_path)
    changed = json.loads(run(*audit_args, expected=1).stdout)
    assert any(
        row["path"] == ".agents/skills/example/SKILL.md" and row["kind"] == "unintegrated"
        for row in changed["drift"]["drift"]
    )
    manifest.pop("targets")
    dump_yaml(manifest, project.manifest_path)
    assert json.loads(run(*audit_args).stdout)["passed"] is True

    # A real requested target change must update identity even for the shared root.
    run(*_INSTALL_ARGS, "--target", "grok-build")
    lock = load_yaml(project.root / "apm.lock.yaml")
    assert {row["target"] for row in lock["deployments"]} == {"grok-build"}
    assert json.loads(run(*audit_args).stdout)["passed"] is True

    # The saved default still controls a fresh install without declared intent.
    project = scenario.consumers.create(
        "configured-consumer", dependencies=({"path": str(source.root)},)
    )
    run(*_INSTALL_ARGS)
    lock = load_yaml(project.root / "apm.lock.yaml")
    assert {row["target"] for row in lock["deployments"]} == {"claude"}
    assert json.loads(run(*audit_args).stdout)["passed"] is True

    # Repairing audit does not admit experimental targets into manifest syntax.
    manifest = load_yaml(project.manifest_path)
    manifest["targets"] = ["grok-cloud"]
    dump_yaml(manifest, project.manifest_path)
    rejected = run(*_INSTALL_ARGS, expected=2)
    assert "Unknown target 'grok-cloud'" in rejected.stdout + rejected.stderr


def test_audit_checks_retained_claims_without_replaying_dropped_targets(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _new_scenario(tmp_path / "scenario", apm_binary_path)
    source = scenario.sources.create("example-package")
    expected = _skill("example").encode("ascii")
    scenario.sources.add_skill(source, "example", expected.decode("ascii"))
    project = scenario.consumers.create("consumer", dependencies=({"path": str(source.root)},))

    def run(*args: str, expected_code: int = 0) -> CommandResult:
        before = ArtifactSnapshot.capture(tmp_path)
        result = scenario.runner.run(args, cwd=project.root, env=scenario.environment)
        if args[0] == "audit":
            assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))
        assert result.returncode == expected_code, (result.stdout, result.stderr)
        return result

    audit = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")
    run("experimental", "enable", "grok-cloud")
    run(*_INSTALL_ARGS, "--target", "grok-cloud")
    manifest = load_yaml(project.manifest_path)
    manifest["targets"] = ["copilot"]
    dump_yaml(manifest, project.manifest_path)
    changed = json.loads(run(*audit, expected_code=1).stdout)
    assert any(
        row["path"] == ".agents/skills/example/SKILL.md" and row["kind"] == "unintegrated"
        for row in changed["drift"]["drift"]
    )
    run(*_INSTALL_ARGS)
    grok = project.root / ".grok/skills/example/SKILL.md"
    copilot = project.root / ".agents/skills/example/SKILL.md"
    assert grok.read_bytes() == copilot.read_bytes() == expected
    lock = load_yaml(project.root / "apm.lock.yaml")
    assert {row["target"] for row in lock["deployments"]} == {"copilot", "grok-cloud"}
    clean = json.loads(run(*audit).stdout)
    assert clean["passed"] is True
    coverage = next(
        check["primitive_coverage"]
        for check in clean["checks"]
        if check["name"] == "content-integrity"
    )
    assert {(row["file"], row["target"]) for row in coverage} == {
        (".grok/skills/example/SKILL.md", "grok-cloud"),
        (".agents/skills/example/SKILL.md", "copilot"),
    }

    grok.unlink()
    missing = json.loads(run(*audit, expected_code=1).stdout)
    presence = next(
        check for check in missing["checks"] if check["name"] == "deployed-files-present"
    )
    assert not presence["passed"]
    assert ".grok/skills/example/SKILL.md" in presence["details"]
    # Historical ownership does not authorize replay into a dropped target.
    assert missing["drift"]["drift"] == []
    grok.write_text("# changed historical file\n", encoding="ascii")
    modified = json.loads(run(*audit, expected_code=1).stdout)
    assert any(
        check["name"] == "content-integrity" and not check["passed"] for check in modified["checks"]
    )
    grok.write_bytes(expected)
    assert json.loads(run(*audit).stdout)["passed"] is True
