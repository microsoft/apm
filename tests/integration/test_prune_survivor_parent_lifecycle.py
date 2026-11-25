"""Prune never leaves a surviving row recorded under a parent it removed (#5238).

apm.yml declares a local package that declares a remote one. Dropping the
local package from apm.yml makes it an orphan, but the remote transitive stays
under the remote retention policy. Prune must not delete the parent's lock row
while the kept row still records it as ``resolved_by``: it keeps that parent
chain fail-closed and names it, and the lock then converges.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _new_scenario,
    _publish,
    _run_success,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.promise("P-OWN-orphan-package-selection", "P-INV-I2", "P-INV-I3"),
]


def _rows(project: Path) -> dict[str, dict]:
    lock = project / "apm.lock.yaml"
    data = load_yaml(lock) if lock.exists() else {}
    return {row["repo_url"]: row for row in (data or {}).get("dependencies") or []}


def _local_package(root: Path, name: str, deps: list[str]) -> None:
    root.mkdir(parents=True)
    manifest: dict = {"name": name, "version": "1.0.0"}
    if deps:
        manifest["dependencies"] = {"apm": deps}
    dump_yaml(manifest, root / "apm.yml")
    skill = root / ".apm" / "skills" / f"{name}-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: {name}-skill\ndescription: x\n---\n# {name}\n", "utf-8")


@pytest.mark.parametrize("shape", ["one-remote", "two-remotes", "local-beside-remote"])
def test_prune_keeps_the_parent_a_retained_remote_survivor_still_records(
    tmp_path: Path, apm_binary_path: Path, shape: str
) -> None:
    scenario = _new_scenario(tmp_path / "survivor-parent", apm_binary_path)
    remotes = [_publish(scenario, "guard-kit", instruction="guard")]
    if shape == "two-remotes":
        remotes.append(_publish(scenario, "lint-kit", instruction="lint"))
    consumer = scenario.consumers.create("consumer", targets=("claude",))
    deps = [remote.dependency for remote in remotes]
    if shape == "local-beside-remote":
        deps.append("../b")
        _local_package(consumer.root / "vendor" / "b", "b", [])
    _local_package(consumer.root / "vendor" / "a", "a", deps)
    manifest = load_yaml(consumer.manifest_path)
    manifest.setdefault("dependencies", {})["apm"] = ["./vendor/a"]
    dump_yaml(manifest, consumer.manifest_path)
    env = scenario.repositories.url_rewrite_subprocess_env_many(
        tuple((remote.repository, remote.remote_url) for remote in remotes)
    )
    _run_success(scenario, consumer, _INSTALL_ARGS, environment=env, scenario_id="install")
    installed = _rows(consumer.root)
    children = sorted(url for url, row in installed.items() if row.get("resolved_by") == "_local/a")
    assert len(children) == len(deps), installed
    manifest["dependencies"]["apm"] = []
    dump_yaml(manifest, consumer.manifest_path)

    pruned = scenario.runner.run(("prune",), scenario_id="prune", cwd=consumer.root, env=env)

    output = " ".join((pruned.stdout + pruned.stderr).split())
    assert pruned.returncode == 0, output
    rows = _rows(consumer.root)
    # Nothing a kept parent still declares is lost, and no surviving row
    # records a parent prune removed.
    assert set(children) <= set(rows), output
    assert all(row.get("resolved_by") in (None, *rows) for row in rows.values()), rows
    assert "Retained _local/a:" in output, output
    lock = (consumer.root / "apm.lock.yaml").read_bytes()

    again = scenario.runner.run(("prune",), scenario_id="prune-again", cwd=consumer.root, env=env)
    frozen = scenario.runner.run(
        ("install", "--frozen", "--no-policy"), scenario_id="frozen", cwd=consumer.root, env=env
    )

    assert again.returncode == 0, again.stdout + again.stderr
    assert frozen.returncode == 0, frozen.stdout + frozen.stderr
    assert (consumer.root / "apm.lock.yaml").read_bytes() == lock
