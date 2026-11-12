"""Installed CLI refusal, recovery and audit across selected physical skill roots."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.copilot_plugins.registrar import catalog_path_for
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_unmanaged_skill_preflight_lifecycle import (
    _CHILD_PROVENANCE,
    _lock_receipt,
    _native_bytes,
    _tree_bytes,
)
from tests.unit.copilot_plugins._builders import write_agent_plugin
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]


def _forget_legacy_conflict_claim(lock_path: Path, prefix: str) -> None:
    """Seed the old partial install's absent claim, independently of product helpers."""
    lock = load_yaml(lock_path)
    lock["deployments"] = [
        row
        for row in lock.get("deployments", [])
        if row["value"] != prefix and not row["value"].startswith(prefix + "/")
    ]
    for dependency in lock["dependencies"]:
        dependency["deployed_files"] = [
            value
            for value in dependency.get("deployed_files", [])
            if value != prefix and not value.startswith(prefix + "/")
        ]
        dependency["deployed_file_hashes"] = {
            value: digest
            for value, digest in dependency.get("deployed_file_hashes", {}).items()
            if not value.startswith(prefix + "/")
        }
    dump_yaml(lock, lock_path)


@pytest.mark.parametrize("scope", ["project", "user"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("prior_partial", [False, True])
@pytest.mark.parametrize("layout", ["subskills", "bundle", "native"])
def test_refusal_preserves_plan_then_narrowing_and_correction_converge(
    tmp_path: Path,
    apm_binary_path: Path,
    scope: str,
    reverse: bool,
    prior_partial: bool,
    layout: str,
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    factory = LocalPackageFactory(isolated.work_root)
    earlier = factory.create("a-before-conflict")
    source = factory.create("review" if layout == "native" else "z-review-lint")
    for package, names in ((earlier, ("prepare",)), (source, ("review", "lint"))):
        for name in names:
            factory.add_skill(
                package,
                name,
                f"---\nname: {name}\ndescription: Fixture {name}\n---\n{name} from {package.name}.\n",
            )
        if layout in {"subskills", "native"}:
            (package.root / ".apm").mkdir()
            (package.root / "skills").rename(package.root / ".apm/skills")
    if layout == "native":
        native_skill = source.root / ".apm/skills/review/SKILL.md"
        native_skill.rename(source.root / "SKILL.md")
        native_skill.parent.rmdir()
    targets = ["claude", "codex"] if scope == "user" else ["claude", "copilot", "codex"]
    if reverse:
        targets.reverse()
    consumer = factory.create(
        "consumer",
        targets=targets,
        dependencies=({"path": str(earlier.root)}, {"path": str(source.root)}),
    )
    deploy_root = isolated.home if scope == "user" else consumer.root
    manifest_root = isolated.home / ".apm" if scope == "user" else consumer.root
    manifest_root.mkdir(exist_ok=True)
    manifest_path = manifest_root / "apm.yml"
    if scope == "user":
        manifest_path.write_bytes(consumer.manifest_path.read_bytes())
    env = isolated.subprocess_env(overrides={"NO_COLOR": "1"})
    guard = Path(env["PYTHONPATH"]) / "sitecustomize.py"
    guard.write_text(guard.read_text(encoding="utf-8") + _CHILD_PROVENANCE, encoding="utf-8")
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    flags = ("--global",) if scope == "user" else ()
    receipts = []

    def run(*args: str):
        child_receipt = tmp_path / f"child-{len(receipts)}.json"
        env["APM_TEST_CHILD_RECEIPT"] = str(child_receipt)
        result = runner.run(args, cwd=manifest_root, env=env, scenario_id="3202-complete-plan")
        receipts.append(
            {
                "command": result.command,
                "exit": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "child_receipt": str(child_receipt),
            }
        )
        (tmp_path / "commands.json").write_text(json.dumps(receipts, indent=2) + "\n")
        return result

    def install(*extra: str):
        return run("install", *flags, "--no-policy", *extra)

    if prior_partial:
        baseline = install()
        assert baseline.returncode == 0, baseline
        _forget_legacy_conflict_claim(manifest_root / "apm.lock.yaml", ".claude/skills/review")
    local = deploy_root / ".claude/skills/review/SKILL.md"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"---\nname: review\ndescription: Personal review\n---\nUser content.\n")
    unrelated = deploy_root / ".agents/skills/personal/notes.txt"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"Keep this unrelated user file.\n")
    native_before = _native_bytes(deploy_root)
    manifest_before = manifest_path.read_bytes()
    ownership_before = _lock_receipt(manifest_root)
    source_before = (_tree_bytes(earlier.root), _tree_bytes(source.root))
    for extra in ((), (), ("--frozen",)):
        refused = install(*extra)
        assert refused.returncode != 0, refused
        if not extra or prior_partial:
            output = refused.stdout + refused.stderr
            assert "review" in output and source.name in output
            assert ".claude/skills/review/SKILL.md" in output
            assert "rename" in output.lower() and "[x]" in output
        assert _native_bytes(deploy_root) == native_before
        assert manifest_path.read_bytes() == manifest_before
        assert _lock_receipt(manifest_root) == ownership_before
        assert (_tree_bytes(earlier.root), _tree_bytes(source.root)) == source_before

    if prior_partial:
        managed_lint = deploy_root / ".agents/skills/lint/SKILL.md"
        lint_before = managed_lint.read_bytes()
        managed_lint.write_bytes(lint_before + b"\nManaged edit.\n")
        audit_before = _native_bytes(deploy_root)
        for fmt in ("text", "json", "sarif"):
            audit = run("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", fmt)
            assert audit.returncode != 0, audit
            if fmt == "text":
                assert "unmanaged-conflict" in audit.stdout + audit.stderr
                assert "Rename" in audit.stdout + audit.stderr
            else:
                document = json.loads(audit.stdout)
                serialized = json.dumps(document)
                assert "unmanaged-conflict" in serialized
                assert "modified" in serialized
            assert _native_bytes(deploy_root) == audit_before
            assert _lock_receipt(manifest_root) == ownership_before
            assert manifest_path.read_bytes() == manifest_before
        managed_lint.write_bytes(lint_before)

    manifest = load_yaml(manifest_path)
    selection_key = "targets" if layout == "native" else "skills"
    manifest["dependencies"]["apm"][1][selection_key] = (
        ["codex"] if layout == "native" else ["lint"]
    )
    dump_yaml(manifest, manifest_path)
    narrowed = install()
    assert narrowed.returncode == 0, narrowed
    assert local.read_bytes() == native_before[".claude/skills/review/SKILL.md"]
    assert (deploy_root / ".agents/skills/lint/SKILL.md").exists()
    assert (deploy_root / ".claude/skills/prepare/SKILL.md").exists()
    personal = local.parent.with_name("personal-review")
    local.parent.rename(personal)
    del manifest["dependencies"]["apm"][1][selection_key]
    dump_yaml(manifest, manifest_path)
    corrected = install()
    assert corrected.returncode == 0, corrected
    skill_source = (
        source.root / "SKILL.md"
        if layout == "native"
        else (source.root / (".apm" if layout == "subskills" else "") / "skills/review/SKILL.md")
    )
    for root in (".claude", ".agents"):
        assert (
            deploy_root / root / "skills/review/SKILL.md"
        ).read_bytes() == skill_source.read_bytes()
    assert (personal / "SKILL.md").read_bytes() == native_before[".claude/skills/review/SKILL.md"]
    manifest["dependencies"]["apm"] = []
    dump_yaml(manifest, manifest_path)
    removed = install()
    assert removed.returncode == 0, removed
    assert not (deploy_root / ".agents/skills/review/SKILL.md").exists()
    assert not (deploy_root / ".claude/skills/lint/SKILL.md").exists()
    assert unrelated.read_bytes() == b"Keep this unrelated user file.\n"
    assert (personal / "SKILL.md").read_bytes() == native_before[".claude/skills/review/SKILL.md"]


@pytest.mark.parametrize("reverse", [False, True])
def test_standalone_skill_refuses_before_earlier_dependency_and_force_is_explicit(
    tmp_path: Path,
    apm_binary_path: Path,
    reverse: bool,
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    factory = LocalPackageFactory(isolated.work_root)
    earlier = factory.create("a-before")
    factory.add_skill(
        earlier, "prepare", "---\nname: prepare\ndescription: Prepare\n---\nPrepare.\n"
    )
    source = factory.create("review")
    (source.root / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review\n---\nPackage review.\n"
    )
    targets = ("codex", "copilot", "claude") if reverse else ("claude", "copilot", "codex")
    consumer = factory.create(
        "consumer",
        targets=targets,
        dependencies=({"path": "../a-before"}, {"path": "../review"}),
    )
    conflict = consumer.root / ".agents/skills/review/SKILL.md"
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"Personal shared-root review.\n")
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    env = isolated.subprocess_env()
    before = _native_bytes(consumer.root)
    refused = runner.run(("install", "--no-policy"), cwd=consumer.root, env=env)
    assert refused.returncode != 0, refused
    assert _native_bytes(consumer.root) == before
    assert not (consumer.root / "apm.lock.yaml").exists()
    forced = runner.run(("install", "--no-policy", "--force"), cwd=consumer.root, env=env)
    assert forced.returncode == 0, forced
    assert conflict.read_bytes() == (source.root / "SKILL.md").read_bytes()


def test_native_plugin_registration_preserves_same_named_unmanaged_loose_skill(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Native registration does not turn plugin members into loose-skill claims."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    source = write_agent_plugin(isolated.work_root / "native", name="review", with_mcp=False)
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer", targets=["copilot"], dependencies=({"path": str(source)},)
    )
    local = consumer.root / ".agents/skills/review-skill/SKILL.md"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"Personal loose skill, not a native plugin member.\n")
    before = _native_bytes(consumer.root)
    source_before = _tree_bytes(source)
    env = isolated.subprocess_env()
    guard = Path(env["PYTHONPATH"]) / "sitecustomize.py"
    guard.write_text(guard.read_text(encoding="utf-8") + _CHILD_PROVENANCE, encoding="utf-8")
    env["APM_TEST_CHILD_RECEIPT"] = str(tmp_path / "child-native.json")
    installed = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--no-policy"), cwd=consumer.root, env=env
    )
    assert installed.returncode == 0, installed
    assert local.read_bytes() == before[".agents/skills/review-skill/SKILL.md"]
    assert _tree_bytes(source) == source_before
    modules = consumer.root / "apm_modules"
    catalog = json.loads(catalog_path_for(modules).read_text(encoding="utf-8"))
    (entry,) = catalog["plugins"]
    assert entry["name"] == "review"
    materialized = modules / entry["source"]
    assert (materialized / "skills/review-skill/SKILL.md").read_bytes() == (
        source / "skills/review-skill/SKILL.md"
    ).read_bytes()
    lock = load_yaml(consumer.root / "apm.lock.yaml")
    assert not any(
        record["value"].startswith(".agents/skills/review-skill")
        for record in lock.get("deployments", [])
    )
