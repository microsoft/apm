"""Unsatisfied persisted skill selections preserve the last working deployment (#3204)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_e2e_mode,
]


def _managed_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for base in (root / ".claude" / "skills", root / ".agents" / "skills")
        for path in base.rglob("*")
        if path.is_file()
    }


def _ownership_snapshot(root: Path) -> dict[str, str]:
    """Read persisted ownership independently of the deployed bytes."""
    lock = load_yaml(root / "apm.lock.yaml")
    hashes = {
        deployment["value"]: deployment["content_hash"]
        for deployment in lock.get("deployments", [])
        if deployment["value"].endswith("/SKILL.md")
    }
    dependency_hashes = {
        path: content_hash
        for dependency in lock["dependencies"]
        for path, content_hash in dependency.get("deployed_file_hashes", {}).items()
        if path.endswith("/SKILL.md")
    }
    assert hashes == dependency_hashes
    for deployment in lock.get("deployments", []):
        if deployment["value"] in hashes:
            assert deployment["owners"] == ["../selection-source"]
            assert deployment["active_owner"] == "../selection-source"
    return hashes


@pytest.mark.parametrize(
    ("layout", "source_change"),
    [
        ("bundle", "typo"),
        ("subskills", "removed"),
        ("plugin", "empty-declaration"),
    ],
)
def test_unmatched_persisted_selection_refuses_without_losing_skills(
    tmp_path: Path,
    apm_engine_command: tuple[str, ...],
    layout: str,
    source_change: str,
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    packages = LocalPackageFactory(isolated.work_root)
    source = packages.create("selection-source")
    for name in ("review", "lint"):
        packages.add_skill(source, name, f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n")
    skills_root = source.root / "skills"
    if layout == "subskills":
        (source.root / ".apm").mkdir()
        skills_root = source.root / ".apm" / "skills"
        (source.root / "skills").rename(skills_root)
    plugin_manifest = source.root / ".claude-plugin" / "plugin.json"
    if layout == "plugin":
        source.manifest_path.unlink()
        plugin_manifest.parent.mkdir()
        plugin_manifest.write_text(
            json.dumps({"name": source.name, "version": "0.1.0", "skills": ["./skills/"]}),
            encoding="utf-8",
        )
    source_bytes = {
        name: (skills_root / name / "SKILL.md").read_bytes() for name in ("review", "lint")
    }
    consumer = packages.create(
        "selection-consumer",
        targets=("claude", "copilot"),
        dependencies=({"path": "../selection-source", "skills": ["review", "lint"]},),
    )
    runner = ApmLifecycleRunner(apm_engine_command, timeout_seconds=180)
    env = isolated.subprocess_env()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

    installed = runner.run(
        ("install", "--no-policy"),
        scenario_id="selection-initial-install",
        cwd=consumer.root,
        env=env,
    )
    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    original_managed = _managed_snapshot(consumer.root)
    original_lock = (consumer.root / "apm.lock.yaml").read_bytes()
    assert len(original_managed) == 4
    original_ownership = _ownership_snapshot(consumer.root)
    assert original_ownership == {
        path: f"sha256:{hashlib.sha256(content).hexdigest()}"
        for path, content in original_managed.items()
    }

    manifest = load_yaml(consumer.manifest_path)
    requested = "reveiw" if source_change == "typo" else "review"
    manifest["dependencies"]["apm"][0]["skills"] = [requested]
    dump_yaml(manifest, consumer.manifest_path)
    if source_change == "removed":
        shutil.rmtree(skills_root)
    elif source_change == "empty-declaration":
        plugin_manifest.write_text(
            json.dumps({"name": source.name, "version": "0.1.0", "skills": []}),
            encoding="utf-8",
        )
    retained_managed = dict(original_managed)
    edited_path = ".agents/skills/review/SKILL.md"
    for attempt in range(2):
        if attempt == 1 and source_change == "typo":
            retained_managed[edited_path] += b"\nUser modification.\n"
            (consumer.root / edited_path).write_bytes(retained_managed[edited_path])
        refused = runner.run(
            ("install", "--no-policy"),
            scenario_id=f"selection-unmatched-refusal-{attempt}",
            cwd=consumer.root,
            env=env,
        )
        assert refused.returncode != 0, (refused.stdout, refused.stderr)
        output = refused.stdout + refused.stderr
        assert "selection-source" in output
        assert f"Requested: {requested}" in output
        assert (
            "Available: lint, review" if source_change == "typo" else "Available: (none)"
        ) in output
        assert _managed_snapshot(consumer.root) == retained_managed
        assert _ownership_snapshot(consumer.root) == original_ownership
        assert (consumer.root / "apm.lock.yaml").read_bytes() == original_lock

    if source_change == "typo":
        audit = runner.run(
            ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json"),
            scenario_id="retained-user-edit-audit",
            cwd=consumer.root,
            env=env,
        )
        assert audit.returncode != 0, (audit.stdout, audit.stderr)
        findings = json.loads(audit.stdout)["drift"]["drift"]
        assert any(finding["kind"] == "modified" for finding in findings)
        assert _ownership_snapshot(consumer.root) == original_ownership
        (consumer.root / edited_path).write_bytes(original_managed[edited_path])

    for name, content in source_bytes.items():
        path = skills_root / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    if layout == "plugin":
        plugin_manifest.write_text(
            json.dumps({"name": source.name, "version": "0.1.0", "skills": ["./skills/"]}),
            encoding="utf-8",
        )

    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"]["apm"][0]["skills"] = ["review"]
    dump_yaml(manifest, consumer.manifest_path)
    narrowed = runner.run(
        ("install", "--no-policy"),
        scenario_id="selection-corrected-narrowing",
        cwd=consumer.root,
        env=env,
    )
    assert narrowed.returncode == 0, (narrowed.stdout, narrowed.stderr)
    expected_narrowed = {
        path: content for path, content in original_managed.items() if "/review/" in path
    }
    assert _managed_snapshot(consumer.root) == expected_narrowed
    assert _ownership_snapshot(consumer.root) == {
        path: original_ownership[path] for path in expected_narrowed
    }

    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"]["apm"] = []
    dump_yaml(manifest, consumer.manifest_path)
    removed = runner.run(
        ("install", "--no-policy"),
        scenario_id="selection-dependency-removal",
        cwd=consumer.root,
        env=env,
    )
    assert removed.returncode == 0, (removed.stdout, removed.stderr)
    assert _managed_snapshot(consumer.root) == {}
    assert _ownership_snapshot(consumer.root) == {}


@pytest.mark.parametrize("selection", [None, ["review"]])
def test_empty_source_install_requires_a_satisfiable_selection(
    tmp_path: Path,
    apm_engine_command: tuple[str, ...],
    selection: list[str] | None,
) -> None:
    """Fresh zero-match installs fail, while unfiltered no-skill packages still work."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    packages = LocalPackageFactory(isolated.work_root)
    packages.create("empty-source")
    dependency = {"path": "../empty-source"}
    if selection is not None:
        dependency["skills"] = selection
    consumer = packages.create(
        "empty-consumer", targets=("claude", "copilot"), dependencies=(dependency,)
    )
    runner = ApmLifecycleRunner(apm_engine_command, timeout_seconds=180)
    env = isolated.subprocess_env()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = runner.run(
        ("install", "--no-policy"),
        scenario_id="empty-source-selection",
        cwd=consumer.root,
        env=env,
    )
    if selection is None:
        assert result.returncode == 0, (result.stdout, result.stderr)
    else:
        assert result.returncode != 0, (result.stdout, result.stderr)
        assert "empty-source" in result.stdout + result.stderr
        assert "Available: (none)" in result.stdout + result.stderr
        assert not (consumer.root / "apm.lock.yaml").exists()
    assert _managed_snapshot(consumer.root) == {}
