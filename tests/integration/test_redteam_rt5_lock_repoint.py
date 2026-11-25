"""RT5 red cells (PERF-Bets): a lockfile that re-points a local dependency.

The manifest declares ``../provider``; the lockfile row is edited so its
``local_path`` and ``anchored_local_path`` name another directory on the same
machine. Whatever install mode runs, the deployed bytes must never come from the
lockfile-only directory: install follows the manifest, or refuses without
mutating the project.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_SKILL = "---\nname: review\ndescription: fixture\n---\n{body}\n"


def _snapshot(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("apm_modules", ".git")]
        for name in files:
            path = Path(dirpath) / name
            if path.is_file() and not path.is_symlink():
                out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


@pytest.mark.parametrize("mode", ["install", "frozen"])
@pytest.mark.parametrize("field", ["local_path", "anchored_local_path", "both"])
def test_a_lockfile_cannot_repoint_a_local_dependency(
    tmp_path: Path, apm_binary_path: Path, mode: str, field: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "repoint", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    provider = factory.create("provider", targets=("agent-skills",))
    factory.add_skill(provider, "review", _SKILL.format(body="manifest-source"))
    elsewhere = factory.create("elsewhere", targets=("agent-skills",))
    factory.add_skill(elsewhere, "review", _SKILL.format(body="lockfile-only-source"))
    consumer = factory.create("consumer", dependencies=("../provider",), targets=("agent-skills",))
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    env = isolated.subprocess_env()

    first = runner.run(("install", "--no-policy"), cwd=consumer.root, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    deployed = consumer.root / ".agents" / "skills" / "review" / "SKILL.md"
    assert b"manifest-source" in deployed.read_bytes()

    lockfile = consumer.root / "apm.lock.yaml"
    data = load_yaml(lockfile)
    rows = [row for row in data["dependencies"] if row.get("source") == "local"]
    assert rows, data
    for row in rows:
        if field in ("local_path", "both"):
            row["local_path"] = str(elsewhere.root)
        if field in ("anchored_local_path", "both"):
            row["anchored_local_path"] = str(elsewhere.root)
    dump_yaml(data, lockfile)
    before = _snapshot(consumer.root)

    args = (
        ("install", "--no-policy") if mode == "install" else ("install", "--frozen", "--no-policy")
    )
    result = runner.run(args, cwd=consumer.root, env=env)
    output = result.stdout + result.stderr
    assert b"lockfile-only-source" not in deployed.read_bytes(), output
    if result.returncode != 0:
        after = _snapshot(consumer.root)
        assert {k: v for k, v in after.items() if k != "apm.lock.yaml"} == {
            k: v for k, v in before.items() if k != "apm.lock.yaml"
        }, output
