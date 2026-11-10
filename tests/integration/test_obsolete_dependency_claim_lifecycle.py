"""Installed-CLI recovery of obsolete, already-absent dependency claims (#3281)."""

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
from tests.integration.test_shared_skill_preservation_lifecycle import (
    _scenario as _shared_scenario,
)
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize("historical", ["legacy", "compatibility", "concrete"])
@pytest.mark.parametrize(
    "already_absent", [False, True], ids=["present-then-absent", "already-absent"]
)
def test_ordinary_install_retires_obsolete_absent_dependency_claims(
    tmp_path: Path,
    apm_binary_path: Path,
    user_scope: bool,
    historical: str,
    already_absent: bool,
) -> None:
    """Migrate a real baseline explicitly, repair normally, audit, replay, and remove."""
    scenario = _new_scenario(tmp_path / "scenario", apm_binary_path)
    source = scenario.sources.create("obsolete-source")
    skill = scenario.sources.add_skill(source, "demo", _skill("demo"))
    extra = skill.parent / "extra.md"
    extra.write_text("Old dependency asset.\n", encoding="ascii")
    consumer = scenario.consumers.create(
        "consumer",
        targets=("claude", "codex"),
        dependencies=({"path": str(source.root)},),
    )
    scope_args = ("--global",) if user_scope else ()
    state_root = scenario.isolated.config_root if user_scope else consumer.root
    deploy_root = scenario.isolated.home if user_scope else consumer.root
    if user_scope:
        dump_yaml(load_yaml(consumer.manifest_path), state_root / "apm.yml")
    lock_path = state_root / "apm.lock.yaml"
    roots = (".claude/skills/demo", ".agents/skills/demo")
    obsolete = {f"{root}/extra.md" for root in roots}
    surviving = {f"{root}/SKILL.md" for root in roots}
    expected_bytes = skill.read_bytes()
    expected_hash = "sha256:" + hashlib.sha256(expected_bytes).hexdigest()

    def run(*args: str, expected: int = 0, cwd: Path | None = None) -> CommandResult:
        result = scenario.runner.run(
            args,
            scenario_id=f"obsolete-claims-{historical}-{user_scope}-{args[0]}",
            cwd=cwd if cwd is not None else consumer.root,
            env=scenario.environment,
        )
        assert result.returncode == expected, (result.command, result.stdout, result.stderr)
        return result

    def audit(*, ci: bool, expected: int = 0) -> dict:
        before = ArtifactSnapshot.capture(tmp_path)
        args = ("audit", "--format", "json")
        if ci:
            args += ("--ci", "--no-policy", "--no-fail-fast")
        result = run(*args, expected=expected, cwd=state_root)
        assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))
        return json.loads(result.stdout)

    install = (*_INSTALL_ARGS, *scope_args)
    run(*install)
    baseline = load_yaml(lock_path)
    assert obsolete <= set(baseline["dependencies"][0]["deployed_files"])
    assert obsolete <= {row["value"] for row in baseline["deployments"]}
    for path in obsolete:
        assert (deploy_root / path).read_bytes() == extra.read_bytes()

    # This is an explicit historical-format migration, not current producer output.
    if historical == "legacy":
        for row in baseline["deployments"]:
            if row["value"].startswith(".agents/"):
                row["target"] = "legacy"
        dump_yaml(baseline, lock_path)
    elif historical == "compatibility":
        baseline.pop("deployments")
        dump_yaml(baseline, lock_path)

    extra.unlink()
    if not already_absent and historical != "concrete":
        # Source removal alone cannot authorize deletion of an unattributed file.
        run(*install)
        retained = load_yaml(lock_path)
        shared = ".agents/skills/demo/extra.md"
        assert (deploy_root / shared).read_bytes() == b"Old dependency asset.\n"
        assert shared in retained["dependencies"][0]["deployed_files"]
        assert shared in retained["dependencies"][0]["deployed_file_hashes"]
        assert any(row["value"] == shared for row in retained["deployments"])
        (deploy_root / shared).unlink()
    if already_absent:
        for path in obsolete:
            (deploy_root / path).unlink()
        before = audit(ci=True, expected=1)
        presence = next(c for c in before["checks"] if c["name"] == "deployed-files-present")
        assert presence["passed"] is False
        assert obsolete <= set(presence["details"])

    before_dry_run = ArtifactSnapshot.capture(tmp_path)
    run(*install, "--dry-run")
    assert_unchanged(before_dry_run, ArtifactSnapshot.capture(tmp_path))
    run(*install)
    repaired = load_yaml(lock_path)
    dependency = repaired["dependencies"][0]
    assert obsolete.isdisjoint(dependency["deployed_files"])
    assert obsolete.isdisjoint(dependency["deployed_file_hashes"])
    assert obsolete.isdisjoint(row["value"] for row in repaired["deployments"])
    assert surviving <= set(dependency["deployed_files"])
    for path in surviving:
        assert (deploy_root / path).read_bytes() == expected_bytes
        assert dependency["deployed_file_hashes"][path] == expected_hash
        records = [row for row in repaired["deployments"] if row["value"] == path]
        assert len(records) == 1
        assert records[0]["owners"] == [str(source.root)]
        assert records[0]["active_owner"] == str(source.root)
        assert records[0]["content_hash"] == expected_hash
    assert all(not (deploy_root / path).exists() for path in obsolete)

    assert audit(ci=True)["passed"] is True
    audit(ci=False)
    settled_bytes = lock_path.read_bytes()
    for flags in ((), ("--frozen",)):
        run(*install, *flags)
        assert lock_path.read_bytes() == settled_bytes
        assert all((deploy_root / path).read_bytes() == expected_bytes for path in surviving)
        assert audit(ci=True)["passed"] is True

    run("uninstall", *scope_args, str(source.root))
    assert all(not (deploy_root / path).exists() for path in surviving | obsolete)
    if lock_path.exists():
        final = load_yaml(lock_path)
        assert not final.get("dependencies")
        assert not final.get("deployments")


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize(
    "protection",
    [
        "edited-marker",
        "unprocessed-target",
        "required-selection",
        "untracked-content",
        "trusted-untracked-content",
    ],
)
def test_historical_absence_respects_live_preservation_boundaries(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool, protection: str
) -> None:
    """Real CLI receipt production cannot turn missing bytes into blanket authority."""
    scenario = _shared_scenario(tmp_path, apm_binary_path, user_scope)
    scenario.install("a")
    lock_path = scenario.lock_root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    shared = ".agents/skills/s/a.md"
    expected_hash = lock["dependencies"][0]["deployed_file_hashes"][shared]
    # Rebinding claims invalidates the prior local receipt; retain a trusted control.
    if protection != "trusted-untracked-content":
        for row in lock["deployments"]:
            if row["value"].startswith(".agents/"):
                row["target"] = "legacy"
        dump_yaml(lock, lock_path)
    (scenario.packages["a"] / "skills/s/a.md").unlink()
    for root in (".agents", ".claude"):
        (scenario.deploy_root / root / "skills/s/a.md").unlink()

    marker = scenario.deploy_root / ".agents/skills/s/SKILL.md"
    untracked = scenario.deploy_root / ".agents/skills/s/personal.txt"
    if protection == "edited-marker":
        marker.write_bytes(b"User-edited skill marker.\n")
    elif protection == "required-selection":
        scenario.manifest(
            dependencies={
                "apm": [{"path": str(scenario.packages["a"]), "skills": ["no-such-skill"]}]
            }
        )
    elif protection in {"untracked-content", "trusted-untracked-content"}:
        untracked.write_bytes(b"Untracked personal content.\n")
    arguments = ("--target", "claude") if protection == "unprocessed-target" else ()
    expected_exit = 1 if protection == "required-selection" else 0
    prior_bytes = lock_path.read_bytes()
    for _ in range(2):
        result = scenario.run("install", *arguments, expected=expected_exit)
        repaired = load_yaml(lock_path)
        dependency = repaired["dependencies"][0]
        rows = [row for row in repaired["deployments"] if row["value"] == shared]
        if protection == "trusted-untracked-content":
            assert shared not in dependency["deployed_files"]
            assert shared not in dependency["deployed_file_hashes"]
            assert rows == []
        else:
            assert shared in dependency["deployed_files"]
            assert dependency["deployed_file_hashes"][shared] == expected_hash
            assert len(rows) == 1
            assert rows[0]["target"] == "legacy"
            assert rows[0]["owners"] == [str(scenario.packages["a"])]
            assert rows[0]["content_hash"] == expected_hash
            scenario.run("audit", "--ci", "--format", "json", expected=1)
        if protection in {"untracked-content", "trusted-untracked-content"}:
            assert untracked.read_bytes() == b"Untracked personal content.\n"
            personal = ".agents/skills/s/personal.txt"
            assert personal not in dependency["deployed_files"]
            assert personal not in dependency["deployed_file_hashes"]
            assert all(row["value"] != personal for row in repaired["deployments"])
        if protection == "untracked-content":
            assert "no trustworthy local deployment baseline" in " ".join(result.stdout.split())
        assert not (scenario.deploy_root / shared).exists()
        if protection == "edited-marker":
            assert marker.read_bytes() == b"User-edited skill marker.\n"
        elif protection == "required-selection":
            assert lock_path.read_bytes() == prior_bytes


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize("protection", ["managed", "edited", "unprocessed"])
def test_dependency_target_narrowing_keeps_cleanup_and_lock_scope_aligned(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool, protection: str
) -> None:
    """Narrow one dependency, not the consumer, without retaining deleted-file claims."""
    scenario = _shared_scenario(tmp_path, apm_binary_path, user_scope)
    sibling_skill = scenario.packages["b"] / "skills/s"
    sibling_skill.rename(sibling_skill.with_name("other"))
    (sibling_skill.with_name("other") / "SKILL.md").write_text(_skill("other"), encoding="ascii")
    scenario.install("a", "b")
    lock_path = scenario.lock_root / "apm.lock.yaml"
    baseline = load_yaml(lock_path)
    codex = ".agents/skills/s"
    marker = f"{codex}/SKILL.md"
    asset = f"{codex}/a.md"
    owner = str(scenario.packages["a"])
    owned = {marker, asset}
    expected_asset = b"a asset\n"
    baseline_hash = "sha256:" + hashlib.sha256(expected_asset).hexdigest()
    initial_rows = {row["value"]: row for row in baseline["deployments"]}
    assert owned <= initial_rows.keys()
    assert initial_rows[asset]["content_hash"] == baseline_hash
    sibling_rows = {
        value: row
        for value, row in initial_rows.items()
        if str(scenario.packages["b"]) in row["owners"]
    }
    sibling_bytes = {
        value: (scenario.deploy_root / value).read_bytes()
        for value in sibling_rows
        if (scenario.deploy_root / value).is_file()
    }
    untracked = scenario.deploy_root / codex / "personal.txt"
    untracked.write_bytes(b"Untracked personal content.\n")
    if protection == "edited":
        (scenario.deploy_root / asset).write_bytes(b"User-edited asset.\n")

    scenario.manifest(
        targets=["claude", "codex"],
        dependencies={
            "apm": [
                {"path": owner, "targets": ["claude"]},
                {"path": str(scenario.packages["b"])},
            ]
        },
    )
    flags = ("--target", "claude") if protection == "unprocessed" else ()
    before_preview = ArtifactSnapshot.capture(tmp_path)
    scenario.run("install", *flags, "--dry-run")
    assert_unchanged(before_preview, ArtifactSnapshot.capture(tmp_path))
    for _ in range(2):
        scenario.run("install", *flags)
        current = load_yaml(lock_path)
        dependency = next(row for row in current["dependencies"] if row["repo_url"] == "_local/a")
        rows = {row["value"]: row for row in current["deployments"]}
        assert load_yaml(scenario.lock_root / "apm.yml")["targets"] == ["claude", "codex"]
        assert {value: rows[value] for value in sibling_rows} == sibling_rows
        assert all(
            (scenario.deploy_root / value).read_bytes() == data
            for value, data in sibling_bytes.items()
        )
        assert untracked.read_bytes() == b"Untracked personal content.\n"
        assert untracked.relative_to(scenario.deploy_root).as_posix() not in rows
        assert rows[codex] == initial_rows[codex]
        if protection == "managed":
            assert not (scenario.deploy_root / marker).exists()
            assert not (scenario.deploy_root / asset).exists()
            assert owned.isdisjoint(dependency["deployed_files"])
            assert owned.isdisjoint(dependency["deployed_file_hashes"])
            assert owned.isdisjoint(rows)
        elif protection == "edited":
            assert (scenario.deploy_root / asset).read_bytes() == b"User-edited asset.\n"
            assert asset in dependency["deployed_files"]
            assert dependency["deployed_file_hashes"][asset] == baseline_hash
            assert rows[asset] == initial_rows[asset]
        else:
            assert (scenario.deploy_root / asset).read_bytes() == expected_asset
            assert owned <= set(dependency["deployed_files"])
            assert {value: rows[value] for value in owned} == {
                value: initial_rows[value] for value in owned
            }
        result = scenario.run(
            "audit", "--ci", "--format", "json", expected=0 if protection == "managed" else 1
        )
        audit = json.loads(result.stdout)
        assert (
            next(check for check in audit["checks"] if check["name"] == "deployed-files-present")[
                "passed"
            ]
            is True
        )
        if protection != "managed":
            assert (
                next(check for check in audit["checks"] if check["name"] == "drift")["passed"]
                is False
            )
        scenario.run("audit", "--format", "json")

    if protection == "edited":
        (scenario.deploy_root / asset).write_bytes(expected_asset)
    scenario.run("install")
    repaired = load_yaml(lock_path)
    dependency = next(row for row in repaired["dependencies"] if row["repo_url"] == "_local/a")
    assert owned.isdisjoint(dependency["deployed_files"])
    assert owned.isdisjoint(dependency["deployed_file_hashes"])
    assert owned.isdisjoint(row["value"] for row in repaired["deployments"])
    assert (
        next(row for row in repaired["deployments"] if row["value"] == codex) == initial_rows[codex]
    )
    settled = lock_path.read_bytes()
    scenario.run("install", "--frozen")
    assert lock_path.read_bytes() == settled
    scenario.run("audit", "--ci", "--format", "json")
    uninstall = ("uninstall", *(("-g",) if user_scope else ()), owner)
    refused = scenario.runner.run(
        uninstall,
        scenario_id="dependency-target-narrowing-protected-uninstall",
        cwd=scenario.project,
        env=scenario.environment.subprocess_env(),
    )
    assert refused.returncode == 1, (refused.command, refused.stdout, refused.stderr)
    assert "Resolve or remove the listed files" in refused.stdout
    assert untracked.read_bytes() == b"Untracked personal content.\n"
    saved_personal = scenario.deploy_root / "personal.txt"
    untracked.rename(saved_personal)
    removed = scenario.runner.run(
        uninstall,
        scenario_id="dependency-target-narrowing-uninstall",
        cwd=scenario.project,
        env=scenario.environment.subprocess_env(),
    )
    assert removed.returncode == 0, (removed.command, removed.stdout, removed.stderr)
    assert saved_personal.read_bytes() == b"Untracked personal content.\n"
    assert all(
        (scenario.deploy_root / value).read_bytes() == data for value, data in sibling_bytes.items()
    )
    assert not (scenario.deploy_root / ".claude/skills/s/SKILL.md").exists()


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
def test_replacement_table_survives_absent_repair_partial_install_and_narrowing(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool
) -> None:
    """Replacement intent survives full and partial dependency-state repair."""
    scenario = _shared_scenario(tmp_path, apm_binary_path, user_scope)
    scenario.install("a")
    lock_path = scenario.lock_root / "apm.lock.yaml"
    baseline = load_yaml(lock_path)
    roots = (".claude/skills/s", ".agents/skills/s")
    obsolete = {f"{root}/a.md" for root in roots}
    markers = {f"{root}/SKILL.md" for root in roots}
    marker_bytes = (scenario.packages["a"] / "skills/s/SKILL.md").read_bytes()
    marker_hash = "sha256:" + hashlib.sha256(marker_bytes).hexdigest()
    for row in baseline["deployments"]:
        if row["value"].startswith(".agents/"):
            row["target"] = "legacy"
    dump_yaml(baseline, lock_path)
    (scenario.packages["a"] / "skills/s/a.md").unlink()
    for path in obsolete:
        (scenario.deploy_root / path).unlink()
    rule = {
        "for": {"git": "https://github.com/unused-replacement-fixture/base.git"},
        "with": {"path": "../intentionally-absent"},
    }
    scenario.manifest(
        **{
            "$schema": (
                "https://microsoft.github.io/apm/specs/schemas/"
                "manifest-source-replacements-v1.schema.json"
            ),
            "replace": [rule],
        }
    )
    scenario.run("install")
    repaired = load_yaml(lock_path)
    dependency = repaired["dependencies"][0]
    assert repaired["replacements"] == [rule]
    assert obsolete.isdisjoint(dependency["deployed_files"])
    assert obsolete.isdisjoint(dependency["deployed_file_hashes"])
    assert obsolete.isdisjoint(row["value"] for row in repaired["deployments"])
    assert dependency["deployed_file_hashes"] == dict.fromkeys(markers, marker_hash)
    assert all((scenario.deploy_root / path).read_bytes() == marker_bytes for path in markers)
    assert all(not (scenario.deploy_root / path).exists() for path in obsolete)
    scenario.run("audit", "--ci", "--format", "json")

    scenario.manifest(replace=[])
    before_refusal = ArtifactSnapshot.capture(tmp_path)
    refused = scenario.run("install", str(scenario.packages["a"]), expected=1)
    assert "Replacement rules changed during a" in refused.stdout
    assert_unchanged(before_refusal, ArtifactSnapshot.capture(tmp_path))
    scenario.run("install")
    assert load_yaml(lock_path)["replacements"] == []
    scenario.install("a", target=None)
    assert load_yaml(lock_path)["replacements"] == []
    scenario.manifest(
        targets=["claude", "codex"],
        dependencies={"apm": [{"path": str(scenario.packages["a"]), "targets": ["claude"]}]},
    )
    scenario.run("install")
    narrowed = load_yaml(lock_path)
    expected = {".claude/skills/s", ".claude/skills/s/SKILL.md"}
    assert narrowed["replacements"] == []
    assert set(narrowed["dependencies"][0]["deployed_files"]) == expected
    assert narrowed["dependencies"][0]["deployed_file_hashes"] == {
        ".claude/skills/s/SKILL.md": marker_hash
    }
    assert {row["value"] for row in narrowed["deployments"]} == expected
    assert not (scenario.deploy_root / ".agents/skills/s").exists()
    assert (scenario.deploy_root / ".claude/skills/s/SKILL.md").read_bytes() == marker_bytes
    assert load_yaml(scenario.lock_root / "apm.yml")["targets"] == ["claude", "codex"]
    settled = lock_path.read_bytes()
    for flags in ((), ("--frozen",)):
        scenario.run("install", *flags)
        assert lock_path.read_bytes() == settled
        scenario.run("audit", "--ci", "--format", "json")
