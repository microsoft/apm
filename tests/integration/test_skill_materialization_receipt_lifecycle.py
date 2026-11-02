"""Installed-CLI preservation controls paired with the unchanged checkout regressions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.integration.test_manifest_version_snapshot_lifecycle import (
    _branch_switch,
    _git,
)

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.requires_apm_binary]


def _receipt(scenario) -> Path:
    receipts = list(
        (Path(scenario.environment["HOME"]) / ".apm/deployment-receipts-v2").glob("*.json")
    )
    assert len(receipts) == 1
    return receipts[0]


@pytest.mark.parametrize("source", ["local", "git-subdirectory"])
@pytest.mark.parametrize("frozen", [False, True], ids=["ordinary", "frozen"])
def test_user_edit_equal_to_old_version_remains_protected(
    tmp_path: Path, apm_binary_path: Path, source: str, frozen: bool
) -> None:
    scenario = _branch_switch(tmp_path, apm_binary_path, source)
    old = scenario.deployed_skill.read_bytes()
    result = scenario.install()
    assert result.returncode == 0, result.stdout + result.stderr
    assert scenario.deployed_skill.read_bytes() != old
    receipt = _receipt(scenario)
    proof = receipt.read_bytes()
    scenario.deployed_skill.write_bytes(old)
    result = scenario.install(*(["--frozen"] if frozen else []))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "modified since deployment" in result.stdout
    assert scenario.deployed_skill.read_bytes() == old
    assert receipt.read_bytes() == proof


@pytest.mark.parametrize("damage", ["missing", "corrupt", "owner", "scope", "identity"])
def test_untrusted_receipt_does_not_replace_legacy_deployment(
    tmp_path: Path, apm_binary_path: Path, damage: str
) -> None:
    scenario = _branch_switch(tmp_path, apm_binary_path, "local")
    old = scenario.deployed_skill.read_bytes()
    receipt = _receipt(scenario)
    if damage == "missing":
        receipt.unlink()
    elif damage == "corrupt":
        receipt.write_text("{", encoding="utf-8")
    else:
        data = json.loads(receipt.read_text(encoding="utf-8"))
        if damage == "owner":
            for row in data["records"]:
                row["active_owner"] = "foreign"
                row["owners"] = ["foreign"]
        else:
            data["scope" if damage == "scope" else "installation"] = "invalid"
        receipt.write_text(json.dumps(data), encoding="utf-8")
    proof = receipt.read_bytes() if receipt.exists() else None
    result = scenario.install()
    assert scenario.deployed_skill.read_bytes() == old
    if damage in {"corrupt", "scope", "identity"}:
        assert result.returncode != 0
        assert "materialization evidence" in result.stdout + result.stderr
    else:
        assert "no trustworthy local deployment baseline" in " ".join(result.stdout.split())
    assert (receipt.read_bytes() if receipt.exists() else None) == proof


@pytest.mark.parametrize(
    "command",
    [
        ("install", "--dry-run", "--target", "claude", "--no-policy"),
        ("audit", "--ci", "--format", "json"),
        ("audit", "--ci", "--no-drift", "--format", "json"),
    ],
    ids=["dry-run", "scratch-audit", "lock-check"],
)
def test_read_only_commands_never_promote_live_receipts(
    tmp_path: Path, apm_binary_path: Path, command: tuple[str, ...]
) -> None:
    scenario = _branch_switch(tmp_path, apm_binary_path, "local")
    state = _receipt(scenario).parent
    before = {path.name: path.read_bytes() for path in state.iterdir()}
    old = scenario.deployed_skill.read_bytes()
    result = scenario.runner.run(
        command,
        scenario_id="receipt-read-only",
        cwd=scenario.root,
        env=scenario.environment,
    )
    assert result.returncode in {0, 1}, result.stdout + result.stderr
    if command[0] == "audit":
        assert isinstance(json.loads(result.stdout), dict)
    else:
        assert "dry-run" in result.stdout.lower()
    assert {path.name: path.read_bytes() for path in state.iterdir()} == before
    assert scenario.deployed_skill.read_bytes() == old
    assert _git(scenario.root, scenario.environment, "ls-files", ".apm-local") == ""


def test_recreated_checkout_with_retained_outputs_has_no_old_authority(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    import shutil

    scenario = _branch_switch(tmp_path, apm_binary_path, "local")
    old = scenario.deployed_skill.read_bytes()
    previous = scenario.root.with_name("previous-consumer")
    scenario.root.rename(previous)
    shutil.copytree(previous, scenario.root, ignore=shutil.ignore_patterns(".apm-local"))
    result = scenario.install()
    assert scenario.deployed_skill.read_bytes() == old
    assert "no trustworthy local deployment baseline" in " ".join(result.stdout.split())
    assert not (scenario.root / ".apm-local/installation").exists()


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
def test_receipts_capture_rendered_links_and_preserve_repeat_bytes(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool
) -> None:
    from tests.integration.test_shared_skill_preservation_lifecycle import _scenario

    scenario = _scenario(tmp_path, apm_binary_path, user_scope)
    package = scenario.packages["a"]
    (package / "DOCUMENT.md").write_text("package document\n", encoding="utf-8")
    source = package / "skills/s/SKILL.md"
    source.write_text(
        "---\nname: s\ndescription: Rendered receipt\n---\n\nRead [document](../../DOCUMENT.md).\n",
        encoding="utf-8",
    )
    scenario.install("a")
    lock = scenario.lock_root / "apm.lock.yaml"
    before = lock.read_bytes()
    receipts = list((scenario.environment.home / ".apm/deployment-receipts-v2").glob("*.json"))
    assert len(receipts) == 1
    data = json.loads(receipts[0].read_text(encoding="utf-8"))
    rows = [row for row in data["records"] if row["value"].endswith("SKILL.md")]
    assert len(rows) == 2
    raw_hash = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    for row in rows:
        deployed = scenario.deploy_root / row["value"]
        assert deployed.read_bytes() != source.read_bytes()
        assert row["content_hash"] == "sha256:" + hashlib.sha256(deployed.read_bytes()).hexdigest()
        assert row["content_hash"] != raw_hash
    proof = receipts[0].read_bytes()
    scenario.install(target=None)
    assert lock.read_bytes() == before
    assert receipts[0].read_bytes() == proof


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
def test_unchanged_shared_target_handoff_retains_update_authority(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool
) -> None:
    from tests.integration.test_shared_skill_preservation_lifecycle import _scenario, _skill

    scenario = _scenario(tmp_path, apm_binary_path, user_scope)
    scenario.install("a", target="copilot")
    deployed = scenario.deploy_root / ".agents/skills/s/SKILL.md"
    before = deployed.read_bytes()
    scenario.manifest(targets=["codex"])
    scenario.install(target=None)
    assert deployed.read_bytes() == before
    replacement = _skill("updated a")
    (scenario.packages["a"] / "skills/s/SKILL.md").write_bytes(replacement)
    result = scenario.install(target=None)
    assert deployed.is_file(), result.stdout + result.stderr
    assert deployed.read_bytes() == replacement, result.stdout + result.stderr
    scenario.run("audit", "--ci")


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
def test_uninstall_last_writer_records_actual_survivor_materialization(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool
) -> None:
    from tests.integration.test_shared_skill_preservation_lifecycle import _scenario, _skill

    scenario = _scenario(tmp_path, apm_binary_path, user_scope)
    for owner, package in scenario.packages.items():
        (package / f"skills/s/{owner}.md").unlink()
    scenario.install("a", "b")
    result = scenario.runner.run(
        ("uninstall", *(("-g",) if user_scope else ()), str(scenario.packages["b"])),
        scenario_id="receipt-survivor-uninstall",
        cwd=scenario.project,
        env=scenario.environment.subprocess_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    deployed = [
        scenario.deploy_root / root / "SKILL.md"
        for root in (".claude/skills/s", ".agents/skills/s")
    ]
    assert [path.read_bytes() for path in deployed] == [_skill("a")] * 2
    replacement = _skill("updated a")
    (scenario.packages["a"] / "skills/s/SKILL.md").write_bytes(replacement)
    result = scenario.install(target=None)
    assert all(path.is_file() for path in deployed), result.stdout + result.stderr
    assert [path.read_bytes() for path in deployed] == [replacement] * 2
    scenario.run("audit", "--ci")
