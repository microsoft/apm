"""Proposed scoped-inline consent, without invoking or enabling a native host."""

import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_frontmatter, load_yaml
from tests.integration.conftest import apm_binary_path as apm_binary_path
from tests.integration.test_inline_hook_consent_lifecycle import _primitive
from tests.spec_conformance._helpers import load_json_fixture
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]


@pytest.mark.req("req-sc-016")
@pytest.mark.parametrize("kind", ["command", "skill", "agent"])
@pytest.mark.parametrize("user_scope", [False, True])
@pytest.mark.parametrize("posture", ["gate-disabled", "approved", "pending", "denied"])
def test_scoped_inline_consent_matches_installed_and_audited_state(
    tmp_path: Path, apm_binary_path: Path, kind: str, user_scope: bool, posture: str
) -> None:
    oracle = load_json_fixture("inline-hooks", "req-sc-016.json")
    assert oracle["spec_anchor"] == "req-sc-016"
    expected = oracle["expected"][posture]
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    source = factory.create("guarded")
    protected_source = _primitive(factory, source, kind, "protected", hooks=True)
    assert load_frontmatter(protected_source).metadata["hooks"] == oracle["hooks"]
    _primitive(factory, source, kind, "plain", hooks=False)
    consumer = factory.create("consumer", targets=("claude",))
    workspace = isolated.config_root if user_scope else consumer.root
    deployment = isolated.home if user_scope else consumer.root
    identity = source.root.as_posix() if user_scope else "../guarded"
    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"] = {"apm": [{"path": identity}]}
    if posture != "gate-disabled":
        manifest["executables"] = {
            "allow": {identity: {"hooks": True}} if posture in {"approved", "denied"} else {}
        }
        if posture == "denied":
            manifest["executables"]["deny"] = {identity: {"hooks": True}}
    dump_yaml(manifest, workspace / "apm.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    installed = runner.run(
        ("install", "--no-policy", *(("--global",) if user_scope else ())),
        cwd=workspace,
        env=isolated.subprocess_env(),
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    native_root = deployment / ".claude"
    markdown = list(native_root.rglob("*.md"))
    assert any("plain" in path.as_posix() for path in markdown)
    protected = [path for path in markdown if "protected" in path.as_posix()]
    assert bool(protected) is expected["deployed"]
    if protected:
        assert load_frontmatter(protected[0]).metadata["hooks"] == oracle["hooks"]
    assert not (native_root / "settings.json").exists()
    lock = LockFile.read(workspace / "apm.lock.yaml")
    assert lock is not None
    assert [record.exec_status for record in lock.dependencies.values()] == [
        expected["exec_status"]
    ]

    def snapshot() -> dict[Path, bytes]:
        return {
            path: path.read_bytes()
            for root in (workspace, native_root)
            for path in root.rglob("*")
            if path.is_file()
        }

    before = snapshot()
    audited = runner.run(
        ("audit", "--ci", "--no-policy"), cwd=workspace, env=isolated.subprocess_env()
    )
    assert audited.returncode == 0, audited.stdout + audited.stderr
    assert snapshot() == before
