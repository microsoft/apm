"""#3441: an empty pre-existing skill directory must not silently skip the install."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
]

_SKILL = b"---\nname: review\ndescription: Fixture review\n---\n# review\nFrom the package.\n"
_DEPLOYED = ".agents/skills/review"


def _consumer(tmp_path: Path, layout: str):
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    factory = LocalPackageFactory(isolated.work_root)
    source = factory.create("review")
    if layout == "native":
        (source.root / "SKILL.md").write_bytes(_SKILL)
    else:
        skill_dir = source.root / ".apm" / "skills" / "review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(_SKILL)
    consumer = factory.create(
        "consumer", targets=("copilot",), dependencies=({"path": str(source.root)},)
    )
    return isolated, consumer


def _install(isolated: IsolatedApmEnvironment, root: Path, apm_binary_path: Path):
    env = isolated.subprocess_env(overrides={"NO_COLOR": "1"})
    return ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--no-policy"), cwd=root, env=env, scenario_id="3441-empty-skill-dir"
    )


def _deployment_values(root: Path) -> list[str]:
    lock = load_yaml(root / "apm.lock.yaml")
    return [row["value"] for row in lock.get("deployments", [])]


@pytest.mark.parametrize("layout", ["subskills", "native"])
def test_install_deploys_into_empty_nested_skill_directory(
    tmp_path: Path, apm_binary_path: Path, layout: str
) -> None:
    isolated, consumer = _consumer(tmp_path, layout)
    (consumer.root / _DEPLOYED / "nested" / "deeper").mkdir(parents=True)

    result = _install(isolated, consumer.root, apm_binary_path)

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert (consumer.root / _DEPLOYED / "SKILL.md").read_bytes() == _SKILL, output
    assert _DEPLOYED in _deployment_values(consumer.root), output
    assert "skipped" not in output, output


@pytest.mark.parametrize("layout", ["subskills", "native"])
def test_install_still_refuses_skill_directory_with_unmanaged_file(
    tmp_path: Path, apm_binary_path: Path, layout: str
) -> None:
    isolated, consumer = _consumer(tmp_path, layout)
    local = consumer.root / _DEPLOYED / "SKILL.md"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"# Hand-authored project skill\n")

    result = _install(isolated, consumer.root, apm_binary_path)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert local.read_bytes() == b"# Hand-authored project skill\n", output
    assert "[x]" in output, output
