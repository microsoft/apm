"""Prune never deletes what a surviving manifest still declares, on generated graphs (#4989).

PROMISE: P-OWN-orphan-package-selection -- ``apm prune`` removes a local
package only when no surviving manifest declares it; every row it keeps
without a surviving declaration is named (source: #4989 scope record).
CELL: OP=prune SRC=local PRIM=skill SCOPE=project LOCK=consistent
GRAPH=generated: small random DAGs of local packages with symlink aliases,
respelled paths and shared descendants; one declared root is dropped and one
surviving manifest loses an edge before prune.

The oracle is a plain breadth-first walk over the generator's own record of
the surviving manifests; it never calls prune's reachability owner. Seeds
are fixed, plus one random seed per run that is printed with any failure.
"""

from __future__ import annotations

import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.utils.yaml_io import dump_yaml, load_yaml

pytestmark = [
    pytest.mark.integration,
    pytest.mark.component,
    pytest.mark.promise("P-OWN-orphan-package-selection", "P-INV-I7"),
]

_FIXED_SEEDS = tuple(range(200))
_RUN_SEED = random.SystemRandom().randrange(1_000_000, 2**31)
_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"


@dataclass
class _Graph:
    """The generator's own record: names, spelled edges and declared roots."""

    names: list[str]
    edges: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)
    aliased: set[str] = field(default_factory=set)
    motif: tuple[str, str] | None = None


def _spell(rng: random.Random, graph: _Graph, target: str) -> str:
    style = rng.choice(("plain", "alias", "respelled"))
    if style == "alias":
        graph.aliased.add(target)
        return f"../{target}-link"
    if style == "respelled":
        return f"../../vendor/{target}"
    return f"../{target}"


def _generate(seed: int) -> _Graph:
    rng = random.Random(seed)  # noqa: S311 -- reproducible test graphs
    names = [f"p{index}" for index in range(rng.randint(4, 8))]
    graph = _Graph(names=names, edges={name: [] for name in names})
    tops: set[str] = set()
    if len(names) >= 6 and rng.random() < 0.5:
        # A kept parent chain whose top later drops it: d -> e -> {f, h} and
        # a -> z -> f through an alias, so f's recorded parent chain stays.
        d, a, e, z, f, h = (names[index] for index in sorted(rng.sample(range(len(names)), 6)))
        for parent, child, spelled in (
            (d, e, f"../{e}"),
            (e, f, f"../{f}"),
            (e, h, f"../{h}"),
            (a, z, f"../{z}"),
            (z, f, f"../{f}-link"),
        ):
            graph.edges[parent].append((child, spelled))
        graph.aliased.add(f)
        graph.motif = (d, e)
        tops = {d, a}
    for low, parent in enumerate(names):
        for child in names[low + 1 :]:
            planted = {planted for planted, _ in graph.edges[parent]}
            if child not in tops and child not in planted and rng.random() < 0.35:
                graph.edges[parent].append((child, _spell(rng, graph, child)))
    # A shared descendant: one package declared by two earlier packages.
    shared = rng.choice([name for name in names[2:] if name not in tops])
    for parent in rng.sample(names[: names.index(shared)], 2):
        if shared not in {child for child, _ in graph.edges[parent]}:
            graph.edges[parent].append((shared, _spell(rng, graph, shared)))
    children = {child for edges in graph.edges.values() for child, _ in edges}
    graph.roots = [name for name in names if name not in children]
    if len(graph.roots) < 2:
        graph.roots.append(rng.choice([name for name in names if name not in graph.roots]))
    return graph


def _write_package(vendor: Path, graph: _Graph, name: str) -> None:
    manifest: dict = {"name": name, "version": "1.0.0"}
    if graph.edges[name]:
        manifest["dependencies"] = {"apm": [spelled for _, spelled in graph.edges[name]]}
    (vendor / name).mkdir(parents=True, exist_ok=True)
    dump_yaml(manifest, vendor / name / "apm.yml")
    skill = vendor / name / ".apm" / "skills" / f"{name}-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        f"---\nname: {name}-skill\ndescription: Skill of {name}\n---\n# {name}\n",
        encoding="utf-8",
    )


def _write_project(project: Path, roots: list[str]) -> None:
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": []},
            "devDependencies": {"apm": [f"./vendor/{root}" for root in roots]},
        },
        project / "apm.yml",
    )
    clear_apm_yml_cache()


def _needed(graph: _Graph, kept: frozenset[str] = frozenset()) -> set[str]:
    """Plain BFS over the surviving manifests: every package still declared.

    Surviving manifests are the declared roots' and those of packages prune
    keeps and names (a kept package's manifest still declares its children).
    """
    seen = {*graph.roots, *kept}
    queue = sorted(seen)
    while queue:
        for child, _ in graph.edges[queue.pop(0)]:
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen


def _run(project: Path, *args: str):
    os.chdir(project)
    with patch(_PATCH_UPDATES, return_value=None):
        return CliRunner().invoke(cli, list(args))


def _rows(project: Path) -> list[dict]:
    lock = load_yaml(project / "apm.lock.yaml") or {}
    return list(lock.get("dependencies") or [])


def _check_seed(seed: int, root: Path) -> str | None:
    """None when prune kept every declared package, "skipped" when install refused
    the graph, else the failure with its seed first."""
    graph = _generate(seed)
    rng = random.Random(seed ^ 0x5EED)  # noqa: S311 -- reproducible test graphs
    project = root / "proj"
    vendor = project / "vendor"
    for name in graph.names:
        _write_package(vendor, graph, name)
    for name in sorted(graph.aliased):
        (vendor / f"{name}-link").symlink_to(name, target_is_directory=True)
    (project / ".claude").mkdir()
    _write_project(project, graph.roots)
    installed = _run(project, "install")
    if installed.exit_code != 0:
        return "skipped"  # install's own limits on this graph are not prune's promise
    dropped = graph.motif[0] if graph.motif else rng.choice(graph.roots)
    graph.roots.remove(dropped)
    _write_project(project, graph.roots)
    # One manifest loses an edge: the planted top its chain edge, else any.
    if graph.motif:
        top, child = graph.motif
        graph.edges[top] = [edge for edge in graph.edges[top] if edge[0] != child]
        _write_package(vendor, graph, top)
    elif edited := [name for name in graph.names if graph.edges[name]]:
        parent = rng.choice(edited)
        graph.edges[parent].pop(rng.randrange(len(graph.edges[parent])))
        _write_package(vendor, graph, parent)
    clear_apm_yml_cache()

    pruned = _run(project, "prune")

    output = " ".join(pruned.output.split())
    rows = _rows(project)
    named = frozenset(row.get("name") for row in rows if f"Retained {row['repo_url']}:" in output)
    needed = _needed(graph, named)
    for name in sorted(needed):
        if not any(row.get("name") == name for row in rows):
            return f"seed {seed}: lock row of declared {name} deleted; {output}"
        if not (project / ".claude" / "skills" / f"{name}-skill" / "SKILL.md").is_file():
            return f"seed {seed}: deployed skill of declared {name} deleted; {output}"
    if pruned.exit_code != 0:
        return f"seed {seed}: prune exited {pruned.exit_code}; {output}"
    for row in rows:
        if row.get("name") not in needed:
            return f"seed {seed}: {row['repo_url']} kept without a name; {output}"
    return None


@pytest.mark.skipif(sys.platform == "win32", reason="needs an unprivileged symlink")
def test_prune_never_deletes_a_declared_package_on_generated_graphs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    print(f"generated-graph run seed: {_RUN_SEED}")
    failures = []
    skipped = 0
    for seed in (*_FIXED_SEEDS, _RUN_SEED):
        root = tmp_path / f"s{seed}"
        root.mkdir()
        failure = _check_seed(seed, root)
        if failure == "skipped":
            skipped += 1
        elif failure is not None:
            failures.append(failure[:1500])
    monkeypatch.chdir(tmp_path)
    print(f"generated graphs: {len(_FIXED_SEEDS) + 1}, install refused: {skipped}")

    assert not failures, f"{len(failures)} failing seed(s):\n" + "\n".join(failures)
    # Most graphs must reach prune, or the cell proves nothing.
    assert skipped <= len(_FIXED_SEEDS) // 4, skipped
