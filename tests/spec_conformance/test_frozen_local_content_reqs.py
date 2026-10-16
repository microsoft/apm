"""Executable coverage for the proposed frozen local-content contract."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.e2e


@pytest.mark.req("req-lk-023")
@pytest.mark.parametrize("transitive", [False, True], ids=["direct", "transitive"])
@pytest.mark.parametrize("failure", ["changed-content", "legacy-baseline"])
def test_frozen_local_content_preserves_prior_evidence(
    tmp_path: Path, transitive: bool, failure: str
) -> None:
    """A real CLI must refuse unverified local content without rebasing trust."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "contract", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    source = factory.create("provider", targets=("agent-skills",))
    skill = factory.add_skill(
        source, "contract", "---\nname: contract\ndescription: fixture\n---\naccepted\n"
    )
    factory.create("parent", dependencies=("../provider",))
    consumer = factory.create(
        "consumer",
        dependencies=("../parent" if transitive else "../provider",),
        targets=("agent-skills",),
    )
    src = Path(__file__).resolve().parents[2] / "src"
    runner = ApmLifecycleRunner(
        (
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(src)!r}); from apm_cli.cli import cli; cli()",
        )
    )
    env = isolated.subprocess_env()
    installed = runner.run(("install", "--no-policy"), cwd=consumer.root, env=env)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    lock_path = consumer.root / "apm.lock.yaml"
    accepted_lock = lock_path.read_bytes()
    accepted_skill = skill.read_bytes()
    if failure == "changed-content":
        skill.write_bytes(accepted_skill.replace(b"accepted", b"changed"))
    else:
        lock = load_yaml(lock_path)
        entry = next(d for d in lock["dependencies"] if d.get("local_path") == "../provider")
        entry.pop("content_hash")
        dump_yaml(lock, lock_path)
    before = {
        p.relative_to(consumer.root).as_posix(): p.read_bytes()
        for p in consumer.root.rglob("*")
        if p.is_file()
    }

    refused = runner.run(("install", "--frozen", "--no-policy"), cwd=consumer.root, env=env)

    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "provider" in refused.stdout + refused.stderr
    assert {
        p.relative_to(consumer.root).as_posix(): p.read_bytes()
        for p in consumer.root.rglob("*")
        if p.is_file()
    } == before
    skill.write_bytes(accepted_skill)
    lock_path.write_bytes(accepted_lock)
    recovered = runner.run(("install", "--frozen", "--no-policy"), cwd=consumer.root, env=env)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert lock_path.read_bytes() == accepted_lock
