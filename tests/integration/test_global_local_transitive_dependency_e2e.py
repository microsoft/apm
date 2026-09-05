"""Installed-CLI regressions for local declaring-parent anchors at user scope."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
]


@pytest.mark.parametrize(
    ("user_scope", "relative_child"),
    [(False, True), (True, False), (True, True)],
    ids=["project-relative-control", "user-absolute-control", "user-relative-regression"],
)
def test_local_transitive_scope_parity(
    tmp_path: Path,
    apm_binary_path: Path,
    user_scope: bool,
    relative_child: bool,
) -> None:
    """Success must include both declared packages, regardless of deploy scope."""
    environment = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=os.environ)
    factory = LocalPackageFactory(environment.package_root)
    child = factory.create("child", targets=["cursor"])
    child_reference = "../child" if relative_child else child.root.as_posix()
    parent = factory.create(
        "parent",
        dependencies=[{"path": child_reference}],
        targets=["cursor"],
    )
    sources = {
        package.name: factory.add_command(
            package,
            package.name,
            f"---\ndescription: {package.name} command\n---\n# Command\n"
            f"Distinct {package.name} command body.\n",
        )
        for package in (parent, child)
    }
    assert (parent.root / "../child").resolve() == child.root
    assert child.manifest_path.is_file()
    consumer = environment.work_root / "consumer"
    consumer.mkdir()
    manifest_root = environment.config_root if user_scope else consumer
    manifest = {
        "name": "consumer",
        "version": "0.1.0",
        "targets": ["cursor"],
        "dependencies": {"apm": [{"path": parent.root.as_posix()}]},
    }
    dump_yaml(manifest, manifest_root / "apm.yml")
    deploy_root = environment.home if user_scope else consumer
    runner = ApmLifecycleRunner([str(apm_binary_path)])
    args = ("install", "--target", "cursor", "--no-policy", "--parallel-downloads", "0")
    if user_scope:
        args += ("--global",)

    for iteration in range(2):
        result = runner.run(
            args,
            scenario_id=f"local-transitive-scope-parity-{iteration}",
            cwd=consumer,
            env=environment.subprocess_env(),
        )
        evidence = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        assert result.returncode == 0, evidence
        lockfile = load_yaml(manifest_root / "apm.lock.yaml")
        entries = lockfile["dependencies"]
        assert len(entries) == 2, evidence
        by_repo = {entry["repo_url"]: entry for entry in entries}
        assert set(by_repo) == {"_local/parent", "_local/child"}, evidence
        locked_child = by_repo["_local/child"]
        assert locked_child["local_path"] == child_reference
        assert locked_child["source"] == "local"
        assert locked_child["depth"] == 2
        assert locked_child["resolved_by"] == "_local/parent"
        assert locked_child["declaring_parent"] == parent.root.as_posix()
        assert locked_child["anchored_local_path"] == child.root.as_posix()
        assert load_yaml(manifest_root / "apm.yml") == manifest

        commands = list((deploy_root / ".cursor" / "commands").glob("*.md"))
        assert len(commands) == 2, evidence
        command_bodies = [path.read_text(encoding="utf-8") for path in commands]
        for name, source in sources.items():
            assert sum(f"Distinct {name} command body." in body for body in command_bodies) == 1
            copies = list((manifest_root / "apm_modules").rglob(f"{name}.prompt.md"))
            assert len(copies) == 1
            assert copies[0].read_bytes() == source.read_bytes()
        if user_scope:
            assert not (consumer / ".cursor").exists()
            assert not (consumer / "apm_modules").exists()
            assert not (consumer / "apm.lock.yaml").exists()


def test_direct_user_relative_input_remains_rejected(tmp_path: Path, apm_binary_path: Path) -> None:
    """A real local directory under CWD does not make a direct global ref valid."""
    environment = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=os.environ)
    factory = LocalPackageFactory(environment.work_root)
    factory.create("direct", targets=["cursor"])
    result = ApmLifecycleRunner([str(apm_binary_path)]).run(
        ("install", "./direct", "--global", "--target", "cursor", "--no-policy"),
        cwd=environment.work_root,
        env=environment.subprocess_env(),
    )
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "relative local paths" in output
    assert "absolute path" in output
    assert not (environment.config_root / "apm.lock.yaml").exists()
    assert not (environment.home / ".cursor" / "commands").exists()
