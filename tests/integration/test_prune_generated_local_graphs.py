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
_CHUNK = 25
_CHUNKS = len(_FIXED_SEEDS) // _CHUNK
_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"


@dataclass
class _Graph:
    """The generator's own record: names, spelled edges and declared roots."""

    names: list[str]
    edges: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)
    aliased: set[str] = field(default_factory=set)
    dev_roots: set[str] = field(default_factory=set)
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
    # Production and dev roots mix; a separate stream keeps each seed's graph shape.
    dev = random.Random(seed ^ 0xDE7)  # noqa: S311 -- reproducible test graphs
    graph.dev_roots = {root for root in graph.roots if dev.random() < 0.5}
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


def _write_project(project: Path, graph: _Graph) -> None:
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {
                "apm": [f"./vendor/{root}" for root in graph.roots if root not in graph.dev_roots]
            },
            "devDependencies": {
                "apm": [f"./vendor/{root}" for root in graph.roots if root in graph.dev_roots]
            },
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


_GRAPH_FIELDS = (
    "depth",
    "is_dev",
    "resolved_by",
    "declaring_parent",
    "local_path",
    "anchored_local_path",
)


def _graph_rows(project: Path) -> dict[str, tuple]:
    """Each lock row's graph metadata, the part prune repairs (#5126)."""
    return {
        row["repo_url"]: tuple(row.get(name) for name in _GRAPH_FIELDS) for row in _rows(project)
    }


def _kept_declared(seed, project: Path, rows, needed, exit_code: int, output: str) -> str | None:
    """The first broken promise after prune: a declared package lost, or an unnamed keep."""
    for name in sorted(needed):
        if not any(row.get("name") == name for row in rows):
            return f"seed {seed}: lock row of declared {name} deleted; {output}"
        if not (project / ".claude" / "skills" / f"{name}-skill" / "SKILL.md").is_file():
            return f"seed {seed}: deployed skill of declared {name} deleted; {output}"
    if exit_code != 0:
        return f"seed {seed}: prune exited {exit_code}; {output}"
    unnamed = [row["repo_url"] for row in rows if row.get("name") not in needed]
    return f"seed {seed}: {unnamed[0]} kept without a name; {output}" if unnamed else None


def _converges(seed: int, project: Path) -> str:
    """A fresh resolve of the pruned project records the same graph (#5126).

    The edited manifest's new content hash is install's to record, not prune's.
    """
    settled = _graph_rows(project)
    clear_apm_yml_cache()
    resolved = _run(project, "install", "--no-frozen")
    if resolved.exit_code != 0 or _graph_rows(project) != settled:
        changed = sorted(map(repr, set(_graph_rows(project).items()) ^ set(settled.items())))
        return f"seed {seed}: install --no-frozen after prune changed {changed}"
    return "converged"


def _check_seed(seed: int, root: Path, *, rekeyed: bool = False) -> tuple[str | None, bool | None]:
    """``(verdict, dropped root removed)``.

    The verdict is "converged" when prune kept every declared package and a
    fresh resolve keeps its lock bytes, None when it kept every declared
    package behind a named fail-closed keep or a root another package still
    declares, "rekeyed" for a graph only install re-keys (#5238; with
    *rekeyed* its convergence is checked too), "skipped" when install refused
    the graph, else the failure with its seed first.
    The second value is None when a fail-closed rule is meant to keep the
    dropped root (the planted chain, a promoted survivor, or a root another
    survivor declares), else whether prune removed it.
    """
    graph = _generate(seed)
    rng = random.Random(seed ^ 0x5EED)  # noqa: S311 -- reproducible test graphs
    project = root / "proj"
    vendor = project / "vendor"
    for name in graph.names:
        _write_package(vendor, graph, name)
    for name in sorted(graph.aliased):
        (vendor / f"{name}-link").symlink_to(name, target_is_directory=True)
    (project / ".claude").mkdir()
    _write_project(project, graph)
    installed = _run(project, "install")
    if installed.exit_code != 0:
        return "skipped", None  # install's own limits on this graph are not prune's promise
    dropped = graph.motif[0] if graph.motif else rng.choice(graph.roots)
    graph.roots.remove(dropped)
    _write_project(project, graph)
    # One manifest loses an edge: the planted top its chain edge, else any.
    if graph.motif:
        top, child = graph.motif
        graph.edges[top] = [edge for edge in graph.edges[top] if edge[0] != child]
        _write_package(vendor, graph, top)
    elif edited := [name for name in graph.names if graph.edges[name]]:
        parent = rng.choice(edited)
        graph.edges[parent].pop(rng.randrange(len(graph.edges[parent])))
        _write_package(vendor, graph, parent)
    # Sometimes apm.yml now declares a transitive survivor directly.
    promotable = sorted(_needed(graph) - set(graph.roots))
    promoted = bool(promotable) and rng.random() < 0.3
    if promoted:
        graph.roots.append(rng.choice(promotable))
        _write_project(project, graph)
    clear_apm_yml_cache()

    pruned = _run(project, "prune")

    output = " ".join(pruned.output.split())
    rows = _rows(project)
    named = frozenset(row.get("name") for row in rows if f"Retained {row['repo_url']}:" in output)
    needed = _needed(graph, named)
    removed = (
        None
        if graph.motif or promoted or dropped in _needed(graph)
        else not any(row.get("name") == dropped for row in rows)
    )
    verdict = _kept_declared(seed, project, rows, needed, pruned.exit_code, output)
    if verdict is not None:
        return verdict, removed
    if named or dropped in needed:
        # A fail-closed keep is named; a hand-dropped root another package
        # still declares stays direct until install records it (#3668).
        return None, removed
    if (promoted or graph.aliased & needed) and not rekeyed:
        # A survivor now declared directly, or one reached through an alias
        # spelling, is re-keyed only by install (#5238); the strict companion
        # cell below fails once prune converges on these too.
        return "rekeyed", removed
    return _converges(seed, project), removed


@pytest.mark.skipif(sys.platform == "win32", reason="needs an unprivileged symlink")
@pytest.mark.parametrize("chunk", range(_CHUNKS))
def test_prune_never_deletes_a_declared_package_on_generated_graphs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    """One chunk of the fixed seeds; the last chunk adds one random seed, printed."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    seeds = list(_FIXED_SEEDS[chunk * _CHUNK : (chunk + 1) * _CHUNK])
    if chunk == _CHUNKS - 1:
        run_seed = random.SystemRandom().randrange(1_000_000, 2**31)
        print(f"generated-graph run seed: {run_seed}")
        seeds.append(run_seed)
    failures = []
    skipped = removed = eligible = converged = 0
    for seed in seeds:
        root = tmp_path / f"s{seed}"
        root.mkdir()
        verdict, dropped_removed = _check_seed(seed, root)
        if verdict == "skipped":
            skipped += 1
            continue
        if dropped_removed is not None:
            eligible += 1
            removed += dropped_removed
        if verdict == "converged":
            converged += 1
        elif verdict is not None and verdict != "rekeyed":
            failures.append(verdict[:1500])
    monkeypatch.chdir(tmp_path)
    print(
        f"seeds: {len(seeds)}, install refused: {skipped}, "
        f"root removed: {removed}/{eligible}, converged: {converged}"
    )

    assert not failures, f"{len(failures)} failing seed(s):\n" + "\n".join(failures)
    # Most graphs must reach prune, or the cell proves nothing.
    assert skipped <= len(seeds) // 4, skipped
    # Keeping and naming everything would satisfy the oracle: where no
    # fail-closed rule keeps it, prune must remove the dropped root in at
    # least half of those graphs, and those graphs are not a token few.
    assert eligible >= len(seeds) // 5, eligible
    assert removed * 2 >= eligible, (removed, eligible)
    # The convergence oracle must run on some graphs in every chunk, not none.
    assert converged >= len(seeds) // 10, converged


@pytest.mark.skipif(sys.platform == "win32", reason="needs an unprivileged symlink")
@pytest.mark.xfail(
    strict=True,
    reason="#5238: prune keeps a promoted or alias-spelled survivor's row; install re-keys it",
)
def test_prune_converges_on_graphs_install_rekeys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The graphs the main cell exempts as #5238's: a fresh resolve keeps prune's lock."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    failures = []
    for seed in _FIXED_SEEDS[:_CHUNK]:
        root = tmp_path / f"s{seed}"
        root.mkdir()
        verdict, _ = _check_seed(seed, root, rekeyed=True)
        if verdict not in (None, "converged", "skipped"):
            failures.append(verdict[:500])
    monkeypatch.chdir(tmp_path)

    assert not failures, "\n".join(failures)
