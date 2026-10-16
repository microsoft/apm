"""Frozen local-path content is checked across real install lifecycles."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.lifecycle_smoke]


def _runner() -> ApmLifecycleRunner:
    source = Path(__file__).resolve().parents[2] / "src"
    return ApmLifecycleRunner(
        (
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(source)!r}); from apm_cli.cli import cli; cli()",
        )
    )


@pytest.mark.parametrize("transitive", [False, True], ids=["direct", "transitive"])
def test_frozen_local_path_refuses_changed_snapshot_without_mutation(
    tmp_path: Path, transitive: bool
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "local-frozen", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    provider = factory.create("provider", targets=("agent-skills",))
    original = factory.add_skill(
        provider, "review", "---\nname: review\ndescription: fixture\n---\noriginal\n"
    )
    factory.create("parent", dependencies=("../provider",))
    consumer = factory.create(
        "consumer",
        dependencies=("../parent" if transitive else "../provider",),
        targets=("agent-skills",),
    )
    cold_consumer = isolated.work_root / "cold-consumer"
    cold_consumer.mkdir()
    shutil.copy2(consumer.manifest_path, cold_consumer / "apm.yml")
    runner = _runner()
    env = isolated.subprocess_env()

    def run(root: Path, *args: str):
        return runner.run(args, scenario_id=f"frozen-local-{root.name}", cwd=root, env=env)

    initial = run(consumer.root, "install", "--no-policy")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    lockfile = consumer.root / "apm.lock.yaml"
    locked_bytes = lockfile.read_bytes()
    deployed = consumer.root / ".agents" / "skills" / "review" / "SKILL.md"
    deployed_bytes = deployed.read_bytes()

    # A fresh consumer uses the CLI-generated baseline and unchanged local source.
    shutil.copy2(lockfile, cold_consumer / "apm.lock.yaml")
    cold = run(cold_consumer, "install", "--frozen", "--no-policy")
    assert cold.returncode == 0, cold.stdout + cold.stderr
    assert (
        cold_consumer / ".agents" / "skills" / "review" / "SKILL.md"
    ).read_bytes() == deployed_bytes

    before_deployment = deployed.read_bytes()
    before_files = _snapshot(consumer.root)

    def assert_rejected_without_mutation() -> None:
        rejected = run(consumer.root, "install", "--frozen", "--no-policy")
        assert rejected.returncode != 0, rejected.stdout + rejected.stderr
        assert "local dependency" in rejected.stdout.lower() + rejected.stderr.lower()
        assert "provider" in rejected.stdout + rejected.stderr
        assert lockfile.read_bytes() == locked_bytes
        assert deployed.read_bytes() == before_deployment == deployed_bytes
        assert _snapshot(consumer.root) == before_files

    original.write_text("---\nname: review\ndescription: fixture\n---\nchanged\n", encoding="utf-8")
    assert_rejected_without_mutation()

    changed_cold = isolated.work_root / "changed-cold-consumer"
    changed_cold.mkdir()
    shutil.copy2(consumer.manifest_path, changed_cold / "apm.yml")
    shutil.copy2(lockfile, changed_cold / "apm.lock.yaml")
    cold_rejected = run(changed_cold, "install", "--frozen", "--no-policy")
    assert cold_rejected.returncode != 0, cold_rejected.stdout + cold_rejected.stderr
    assert "local dependency" in cold_rejected.stdout.lower() + cold_rejected.stderr.lower()
    assert not (changed_cold / ".agents").exists()
    assert (changed_cold / "apm.lock.yaml").read_bytes() == locked_bytes

    original.write_bytes(b"---\nname: review\ndescription: fixture\n---\noriginal\n")
    added = provider.root / "references" / "new.md"
    added.parent.mkdir()
    added.write_text("new local content\n", encoding="utf-8")
    assert_rejected_without_mutation()
    added.unlink()

    original.unlink()
    assert_rejected_without_mutation()

    # Restoring the accepted source makes the same locked frozen replay succeed.
    original.write_bytes(b"---\nname: review\ndescription: fixture\n---\noriginal\n")
    recovered = run(consumer.root, "install", "--frozen", "--no-policy")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert lockfile.read_bytes() == locked_bytes
    assert deployed.read_bytes() == deployed_bytes


def _snapshot(root: Path) -> dict[str, bytes]:
    """Read independent byte evidence for all managed files and cached packages."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path != root / "apm.yml"
    }


@pytest.mark.parametrize("transitive", [False, True], ids=["direct", "transitive"])
@pytest.mark.parametrize("baseline", ["missing-entry", "legacy-hash"])
def test_frozen_local_requires_selected_lock_evidence(
    tmp_path: Path, transitive: bool, baseline: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "evidence", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    provider = factory.create("provider", targets=("agent-skills",))
    original = factory.add_skill(
        provider, "review", "---\nname: review\ndescription: fixture\n---\noriginal\n"
    )
    factory.create("parent", dependencies=("../provider",))
    consumer = factory.create(
        "consumer",
        dependencies=("../parent" if transitive else "../provider",),
        targets=("agent-skills",),
    )
    runner = _runner()
    env = isolated.subprocess_env()
    installed = runner.run(("install", "--no-policy"), cwd=consumer.root, env=env)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    lock_path = consumer.root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    provider_entry = next(d for d in lock["dependencies"] if d.get("local_path") == "../provider")
    if baseline == "missing-entry":
        lock["dependencies"].remove(provider_entry)
        original.write_bytes(original.read_bytes().replace(b"original", b"changed"))
    else:
        provider_entry.pop("content_hash")
    dump_yaml(lock, lock_path)
    before = _snapshot(consumer.root)
    result = runner.run(("install", "--frozen", "--no-policy"), cwd=consumer.root, env=env)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "provider" in result.stdout + result.stderr
    assert _snapshot(consumer.root) == before


@pytest.mark.parametrize("transitive", [False, True], ids=["direct", "transitive"])
def test_frozen_local_ignores_removed_dependency_source(tmp_path: Path, transitive: bool) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "orphan", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    provider = factory.create("provider", targets=("agent-skills",))
    factory.add_skill(
        provider, "review", "---\nname: review\ndescription: fixture\n---\noriginal\n"
    )
    parent = factory.create("parent", dependencies=("../provider",))
    survivor = factory.create("survivor", targets=("agent-skills",))
    factory.add_skill(survivor, "kept", "---\nname: kept\ndescription: fixture\n---\nkept\n")
    consumer = factory.create(
        "consumer",
        dependencies=("../parent" if transitive else "../provider", "../survivor"),
        targets=("agent-skills",),
    )
    runner = _runner()
    env = isolated.subprocess_env()
    initial = runner.run(("install", "--no-policy"), cwd=consumer.root, env=env)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    locked = (consumer.root / "apm.lock.yaml").read_bytes()
    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"]["apm"] = [{"path": "../survivor"}]
    dump_yaml(manifest, consumer.manifest_path)
    shutil.rmtree(provider.root)
    shutil.rmtree(parent.root)
    result = runner.run(("install", "--frozen", "--no-policy"), cwd=consumer.root, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (consumer.root / "apm.lock.yaml").read_bytes() == locked
    assert (consumer.root / ".agents/skills/kept/SKILL.md").is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="Requires unprivileged POSIX symlink creation")
@pytest.mark.parametrize("transitive", [False, True], ids=["direct", "transitive"])
def test_frozen_local_refuses_symlink_retarget_without_mutation(
    tmp_path: Path, transitive: bool
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "symlink", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    provider = factory.create("provider", targets=("agent-skills",))
    original = factory.add_skill(
        provider, "review", "---\nname: review\ndescription: fixture\n---\noriginal\n"
    )
    first = provider.root / "first.md"
    second = provider.root / "second.md"
    original.rename(first)
    second.write_bytes(first.read_bytes().replace(b"original", b"changed"))
    original.symlink_to(first)
    factory.create("parent", dependencies=("../provider",))
    consumer = factory.create(
        "consumer",
        dependencies=("../parent" if transitive else "../provider",),
        targets=("agent-skills",),
    )
    runner = _runner()
    env = isolated.subprocess_env()
    initial = runner.run(("install", "--no-policy"), cwd=consumer.root, env=env)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    replay = runner.run(("install", "--frozen", "--no-policy"), cwd=consumer.root, env=env)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    before = _snapshot(consumer.root)
    original.unlink()
    original.symlink_to(second)
    refused = runner.run(("install", "--frozen", "--no-policy"), cwd=consumer.root, env=env)
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "provider" in refused.stdout + refused.stderr
    assert _snapshot(consumer.root) == before

    accepted = runner.run(("install", "--no-policy"), cwd=consumer.root, env=env)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    replay = runner.run(("install", "--frozen", "--no-policy"), cwd=consumer.root, env=env)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert b"changed" in (consumer.root / ".agents/skills/review/SKILL.md").read_bytes()
