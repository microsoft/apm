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
from tests.lifecycle.known import KnownFailure

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


def _graph_rows(project: Path) -> dict[str, dict]:
    """Each package's lock graph metadata by name, the part prune repairs (#5126)."""
    return {
        row.get("name"): {"repo_url": row["repo_url"], **{f: row.get(f) for f in _GRAPH_FIELDS}}
        for row in _rows(project)
    }


def _descendants(graph: _Graph, name: str) -> set[str]:
    seen: set[str] = set()
    queue = [name]
    while queue:
        for child, _ in graph.edges[queue.pop()]:
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen


def _drift(before: dict, after: dict, names) -> list[str]:
    """Each named package whose graph metadata a fresh resolve changed."""
    return [
        f"{name}: {before.get(name)} -> {after.get(name)}"
        for name in sorted(names)
        if before.get(name) != after.get(name)
    ]


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


@dataclass(frozen=True)
class _Exempt:
    """Rows the convergence oracle skips, each for a stated reason."""

    rows: frozenset[str]  # named fail-closed keeps; a hand-dropped root (#3668)
    rekeyed: frozenset[str]  # promoted or alias-spelled survivors: install re-keys (#5238)
    # Rows beneath either: install moves their parent, depth and dev status
    # (or drops them) when it drops or re-keys the row above.
    beneath: frozenset[str]
    unchanged: bool = False  # prune left the lockfile bytes as they were


def _identity(rows: dict, name: str) -> tuple:
    """A package's lock identity: its repo_url and whether it is recorded direct."""
    row = rows.get(name) or {}
    return row.get("repo_url"), row.get("resolved_by") is None


def _recorded_under(rows: dict, exempt: _Exempt) -> set[str]:
    """Rows the pruned lock records beneath an exempt row, through resolved_by.

    While prune keeps a row fail-closed it repairs no parent, so a row still
    recorded under it moves when install drops that row.
    """
    by_url = {row["repo_url"]: name for name, row in rows.items()}
    under = set(exempt.rows | exempt.rekeyed)
    grew = True
    while grew:
        grew = False
        for name, row in rows.items():
            if name not in under and by_url.get(row.get("resolved_by")) in under:
                under.add(name)
                grew = True
    return under


def _rekey_verdict(seed: int, settled: dict, after: dict, names: set[str]) -> str:
    """The companion's verdict on the #5238 rows *names*.

    Only a changed lock identity is the #5238 re-key: a new repo_url, a move
    between a direct and a transitive record (a promotion), or a row recorded
    under such a row. Any other change on these rows is reported unsigned.
    """
    rekeyed_all = {
        name
        for name in set(settled) | set(after)
        if _identity(settled, name) != _identity(after, name)
    }
    rekeyed_urls = {settled[name]["repo_url"] for name in rekeyed_all if name in settled}
    moved = [
        name
        for name in sorted(names)
        if name in rekeyed_all or (settled.get(name) or {}).get("resolved_by") in rekeyed_urls
    ]
    drift = _drift(settled, after, names - set(moved))
    if drift:
        return f"seed {seed}: install --no-frozen after prune changed {drift}"
    return f"seed {seed}: install re-keyed {moved}" if moved else "converged"


def _converges(seed: int, project: Path, exempt: _Exempt, *, rekeyed: bool, tally: dict) -> str:
    """A fresh resolve of the pruned project records the same graph (#5126).

    Every row but the exempt ones is compared; the edited manifest's new
    content hash is install's to record, not prune's. With *rekeyed* only the
    #5238 rows are compared, each failure naming the re-key. *tally* counts
    the rows seen and the rows compared; a graph that compared none is
    "vacuous", never "converged".
    """
    settled = _graph_rows(project)
    tally["lock_before"] = load_yaml(project / "apm.lock.yaml")
    if exempt.unchanged:
        # Prune removed nothing and wrote no lock: the rows are the last
        # install's, not prune's to repair (#5126 repairs after a removal).
        return "unchanged"
    tally["rows"] = tally.get("rows", 0) + len(settled)
    clear_apm_yml_cache()
    resolved = _run(project, "install", "--no-frozen")
    after = _graph_rows(project)
    tally["lock_after"] = load_yaml(project / "apm.lock.yaml")
    if resolved.exit_code != 0:
        return f"seed {seed}: install --no-frozen after prune failed: {resolved.output[:500]}"
    if rekeyed:
        tally["compared"] = tally.get("compared", 0) + len(exempt.rekeyed)
        return _rekey_verdict(seed, settled, after, set(exempt.rekeyed))
    skipped = exempt.rows | exempt.rekeyed | exempt.beneath | _recorded_under(settled, exempt)
    names = (set(settled) | set(after)) - skipped
    tally["compared"] = tally.get("compared", 0) + len(names)
    drift = _drift(settled, after, names)
    if drift:
        return f"seed {seed}: install --no-frozen after prune changed {drift}"
    return "converged" if names else "vacuous"


def _check_seed(
    seed: int, root: Path, *, rekeyed: bool = False, tally: dict | None = None
) -> tuple[str | None, bool | None]:
    """``(verdict, dropped root removed)``.

    The verdict is "converged" when prune kept every declared package and a
    fresh resolve records the same graph for every row the oracle does not
    exempt (with *rekeyed*: for the #5238 rows only) and compared at least one,
    "vacuous" when it compared none, "unchanged" when prune left the lock
    as it was, "skipped" when install refused the graph, else the failure
    with its seed first. *tally* accumulates the rows seen and compared.
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
    edited_manifest = None
    if graph.motif:
        top, child = graph.motif
        graph.edges[top] = [edge for edge in graph.edges[top] if edge[0] != child]
        _write_package(vendor, graph, top)
        edited_manifest = top
    elif edited := [name for name in graph.names if graph.edges[name]]:
        parent = rng.choice(edited)
        graph.edges[parent].pop(rng.randrange(len(graph.edges[parent])))
        _write_package(vendor, graph, parent)
        edited_manifest = parent
    # Sometimes apm.yml now declares a transitive survivor directly.
    promotable = sorted(_needed(graph) - set(graph.roots))
    promoted = rng.choice(promotable) if promotable and rng.random() < 0.3 else None
    if promoted:
        graph.roots.append(promoted)
        _write_project(project, graph)
    clear_apm_yml_cache()

    lock_before = (project / "apm.lock.yaml").read_bytes()
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
    # A fail-closed keep is named; a hand-dropped root another package still
    # declares stays direct until install records it (#3668); a promoted or
    # alias-spelled survivor is re-keyed only by install (#5238, checked by the
    # companion cell below). Every other row is compared on every field.
    rows = frozenset(named | ({dropped} if dropped in needed else set()))
    rekeyed_rows = frozenset(({promoted} if promoted else set()) | (graph.aliased & needed))
    beneath = set().union(*(_descendants(graph, name) for name in rows | rekeyed_rows))
    exempt = _Exempt(
        rows=rows,
        rekeyed=rekeyed_rows,
        beneath=frozenset(beneath - rows - rekeyed_rows),
        unchanged=lock_before == (project / "apm.lock.yaml").read_bytes(),
    )
    tally = {} if tally is None else tally
    tally["edited"] = edited_manifest
    return _converges(seed, project, exempt, rekeyed=rekeyed, tally=tally), removed


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
    tally: dict = {}
    for seed in seeds:
        root = tmp_path / f"s{seed}"
        root.mkdir()
        verdict, dropped_removed = _check_seed(seed, root, tally=tally)
        if verdict == "skipped":
            skipped += 1
            continue
        if dropped_removed is not None:
            eligible += 1
            removed += dropped_removed
        if verdict == "converged":
            converged += 1
        elif verdict not in (None, "unchanged", "vacuous"):
            failures.append(verdict[:1500])
    monkeypatch.chdir(tmp_path)
    print(
        f"seeds: {len(seeds)}, install refused: {skipped}, "
        f"root removed: {removed}/{eligible}, converged: {converged}, "
        f"rows compared: {tally.get('compared', 0)}/{tally.get('rows', 0)}"
    )

    assert not failures, f"{len(failures)} failing seed(s):\n" + "\n".join(failures)
    # Most graphs must reach prune, or the cell proves nothing.
    assert skipped <= len(seeds) // 4, skipped
    # Keeping and naming everything would satisfy the oracle: where no
    # fail-closed rule keeps it, prune must remove the dropped root in at
    # least half of those graphs, and those graphs are not a token few.
    assert eligible >= len(seeds) // 5, eligible
    assert removed * 2 >= eligible, (removed, eligible)
    # The convergence oracle must hold on most graphs, each comparing at least
    # one row, and must compare a real share of all rows, not a token few.
    assert converged * 2 >= len(seeds), converged
    assert tally.get("compared", 0) * 5 >= tally.get("rows", 0) * 2, tally


_REKEYED = KnownFailure(
    "#5238",
    "prune keeps a promoted or alias-spelled survivor's row that install re-keys",
    r"install re-keyed",
)


@pytest.mark.skipif(sys.platform == "win32", reason="needs an unprivileged symlink")
@_REKEYED.mark()
def test_prune_converges_on_graphs_install_rekeys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows the main cell exempts as #5238's: a fresh resolve keeps prune's record.

    Any other failure is reported without the signature, so it fails the cell.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    rekeyed, other = [], []
    for seed in _FIXED_SEEDS[:_CHUNK]:
        root = tmp_path / f"s{seed}"
        root.mkdir()
        verdict, _ = _check_seed(seed, root, rekeyed=True)
        if verdict in (None, "converged", "skipped", "unchanged", "vacuous"):
            continue
        (rekeyed if "install re-keyed" in verdict else other).append(verdict[:500])
    monkeypatch.chdir(tmp_path)

    assert not other, "\n".join(other)
    assert not rekeyed, "\n".join(rekeyed)


@pytest.mark.skipif(sys.platform == "win32", reason="needs an unprivileged symlink")
def test_prune_then_install_keeps_the_diamond_parent_of_seed_398718870(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5386: p4 is declared by p2 and p3 at one depth; the dropped root p0 met p2 first.

    After prune, install met p3 first (p1 declares it first) and recorded p3 as
    p4's declaring_parent while resolved_by stayed p2.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    tally: dict = {}
    verdict, _ = _check_seed(398718870, tmp_path, tally=tally)
    monkeypatch.chdir(tmp_path)

    assert verdict == "converged", verdict
    # The whole lockfile, not only the graph fields: only the content hash of the
    # package whose manifest the scenario edited may change on the final install.
    before, after = tally["lock_before"], tally["lock_after"]
    for lock in (before, after):
        for row in lock["dependencies"]:
            if row.get("name") == tally["edited"]:
                row.pop("content_hash", None)
    assert after == before


def _diamond(project: Path, order: tuple[str, str]) -> None:
    """Root declares a; a declares both *order* parents; each declares d."""
    vendor = project / "vendor"
    graph = _Graph(
        names=["a", "b", "c", "d"],
        edges={"a": [(name, f"../{name}") for name in order], "b": [], "c": [], "d": []},
        roots=["a"],
    )
    graph.edges["b"] = [("d", "../d")]
    graph.edges["c"] = [("d", "../../vendor/d")]
    for name in graph.names:
        _write_package(vendor, graph, name)
    _write_project(project, graph)


@pytest.mark.parametrize("order", [("b", "c"), ("c", "b")], ids=["b-first", "c-first"])
def test_a_diamond_records_one_parent_whatever_the_manifest_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, order: tuple[str, str]
) -> None:
    """#5386: d is declared by b and c at one depth: its row names b for both fields.

    resolved_by, declaring_parent and local_path come from one edge, the
    nearest declaring parent with the lowest key, so no lock row depends on
    which parent a's manifest lists first, and a re-install keeps every row.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    _diamond(project, order)

    installed = _run(project, "install")
    first = _rows(project)
    _diamond(project, tuple(reversed(order)))
    clear_apm_yml_cache()
    reinstalled = _run(project, "install", "--no-frozen")
    second = _rows(project)
    monkeypatch.chdir(tmp_path)
    # a's manifest was rewritten: its content hash is the one row field to change.
    for rows in (first, second):
        next(row for row in rows if row.get("name") == "a").pop("content_hash")

    assert installed.exit_code == 0, installed.output
    assert reinstalled.exit_code == 0, reinstalled.output
    d = _graph_rows(project)["d"]
    assert (d["resolved_by"], d["declaring_parent"], d["local_path"]) == (
        "_local/b",
        "local:vendor/b",
        "../d",
    ), d
    assert second == first


def test_prune_reparents_a_three_parent_survivor_as_install_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5386: a, c and b each declare d; dropping a leaves c (met first) and b (lowest key).

    Prune re-parents d under the parent install records, so install leaves the
    lockfile unchanged.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    graph = _Graph(
        names=["a", "c", "b", "d"],
        edges={"a": [("d", "../d")], "c": [("d", "../d")], "b": [("d", "../d")], "d": []},
        roots=["a", "c", "b"],
    )
    for name in graph.names:
        _write_package(project / "vendor", graph, name)
    _write_project(project, graph)
    installed = _run(project, "install")
    graph.roots.remove("a")
    _write_project(project, graph)

    pruned = _run(project, "prune")
    lock = (project / "apm.lock.yaml").read_bytes()
    clear_apm_yml_cache()
    reinstalled = _run(project, "install", "--no-frozen")
    monkeypatch.chdir(tmp_path)

    assert installed.exit_code == 0, installed.output
    assert pruned.exit_code == 0, pruned.output
    assert reinstalled.exit_code == 0, reinstalled.output
    d = _graph_rows(project)["d"]
    assert d["resolved_by"] == "_local/b", d
    assert d["declaring_parent"] == "./vendor/b", d
    assert (project / "apm.lock.yaml").read_bytes() == lock
