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


def _installed(tmp_path: Path, apm_binary_path: Path, packages: dict[str, list[str]], roots):
    """Install *roots* (apm.yml entries) over local *packages* under vendor/; one remote
    is published and named ``REMOTE`` in a package's dependency list."""
    scenario = _new_scenario(tmp_path / "survivor-parent", apm_binary_path)
    remote = _publish(scenario, "guard-kit", instruction="guard")
    consumer = scenario.consumers.create("consumer", targets=("claude",))
    for name, deps in packages.items():
        declared = [remote.dependency if dep == "REMOTE" else dep for dep in deps]
        _local_package(consumer.root / "vendor" / name, name, declared)
    manifest = load_yaml(consumer.manifest_path)
    manifest.setdefault("dependencies", {})["apm"] = list(roots)
    dump_yaml(manifest, consumer.manifest_path)
    env = scenario.repositories.url_rewrite_subprocess_env_many(
        ((remote.repository, remote.remote_url),)
    )
    _run_success(scenario, consumer, _INSTALL_ARGS, environment=env, scenario_id="install")
    return scenario, consumer, manifest, env


def _prune(scenario, consumer, env) -> tuple[dict[str, dict], str]:
    pruned = scenario.runner.run(("prune",), scenario_id="prune", cwd=consumer.root, env=env)
    output = " ".join((pruned.stdout + pruned.stderr).split())
    assert pruned.returncode == 0, output
    rows = _rows(consumer.root)
    assert all(row.get("resolved_by") in (None, *rows) for row in rows.values()), rows
    return rows, output


def test_prune_keeps_every_recorded_parent_of_a_shared_child_it_keeps(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5449 review: dropped roots a and z both declare c, c is recorded under a, and only
    z declares the remote. Keeping z keeps c, so a stays too: c never dangles."""
    scenario, consumer, manifest, env = _installed(
        tmp_path,
        apm_binary_path,
        {"a": ["../c"], "z": ["../c", "REMOTE"], "c": []},
        ["./vendor/a", "./vendor/z"],
    )
    assert _rows(consumer.root)["_local/c"]["resolved_by"] == "_local/a"
    manifest["dependencies"]["apm"] = []
    dump_yaml(manifest, consumer.manifest_path)

    rows, output = _prune(scenario, consumer, env)

    assert {"_local/a", "_local/z", "_local/c"} <= set(rows), output
    assert "Retained _local/a:" in output, output


def test_prune_walks_a_kept_chain_from_its_root_declared_top(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5449 review: a -> ../e -> {remote, ../h}. The kept chain is walked from a, so e's
    parent-relative paths resolve beside it: h stays, and an unrelated orphan goes."""
    scenario, consumer, manifest, env = _installed(
        tmp_path,
        apm_binary_path,
        {"a": ["../e"], "e": ["REMOTE", "../h"], "h": [], "u": []},
        ["./vendor/a", "./vendor/u"],
    )
    manifest["dependencies"]["apm"] = []
    dump_yaml(manifest, consumer.manifest_path)

    rows, output = _prune(scenario, consumer, env)

    assert {"_local/a", "_local/e", "_local/h"} <= set(rows), output
    assert "_local/u" not in rows, output


def test_prune_keeps_what_a_kept_parent_declares_when_its_chain_no_longer_reaches_it(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5449 review: a stopped declaring e, and a childless package sits at the path e's
    ``../h`` would name from the project root. h stays: e still declares it."""
    scenario, consumer, manifest, env = _installed(
        tmp_path,
        apm_binary_path,
        {"a": ["../e"], "e": ["REMOTE", "../h"], "h": []},
        ["./vendor/a"],
    )
    h_key = next(url for url in _rows(consumer.root) if url.endswith("/h"))
    _local_package(consumer.root.parent / "e", "e", [])  # project_root/../e
    dump_yaml({"name": "a", "version": "1.0.0"}, consumer.root / "vendor" / "a" / "apm.yml")
    manifest["dependencies"]["apm"] = []
    dump_yaml(manifest, consumer.manifest_path)

    rows, output = _prune(scenario, consumer, env)

    assert {"_local/e", h_key} <= set(rows), output
