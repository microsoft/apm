"""No-growth taxonomy, placement and promise-link ratchets for test modules.

"Newly added" is decided against the checked-in ``legacy_modules`` list in
``taxonomy_ratchet_baseline.json``, never against git history, so every
check is deterministic for a given tree. Run
``uv run --frozen python -m tests.quality.taxonomy_ratchets --update-baseline``
to drop stale entries; the updater only accepts reductions.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from scripts.ratchet_baseline import BaselineError, load_baseline, write_baseline

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path("tests") / "quality" / "taxonomy_ratchet_baseline.json"
CATALOG = Path("tests") / "quality" / "promise-catalog.json"
SPEC = Path("docs") / "src" / "content" / "docs" / "specs" / "openapm-v0.1.md"
OWNERS_DIR = Path(".apm") / "architecture" / "owners"
INVENTORY_PLUGIN = "tests.quality.taxonomy_inventory_plugin"
BEHAVIORAL_MARKERS = frozenset({"unit", "component", "e2e"})
UPDATE_COMMAND = "uv run --frozen python -m tests.quality.taxonomy_ratchets --update-baseline"
BASELINE_DESCRIPTION = (
    "Checked-in module lists for the taxonomy ratchets in "
    "tests/quality/test_test_taxonomy.py. legacy_modules are the collected test "
    "modules that existed when the ratchets were introduced; any collected module "
    "not listed is newly added and must declare one behavioral pytestmark, pass "
    "the placement checks and carry pytest.mark.promise ids from "
    "tests/quality/promise-catalog.json. unclassified_modules may only shrink."
)
BASELINE_KEYS = frozenset(
    {"description", "legacy_modules", "schema_version", "unclassified_modules"}
)
CATALOG_SOURCES = frozenset({"architecture-owner", "spec"})
SPEC_ANCHOR = re.compile(r'<a id="(req-[a-z0-9]+-[0-9]+)"></a>')
SPAWN_CALLS: dict[str, frozenset[str]] = {
    "subprocess": frozenset(
        {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
    ),
    "os": frozenset({"system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp"}),
    "asyncio": frozenset({"create_subprocess_exec", "create_subprocess_shell"}),
    "pexpect": frozenset({"spawn", "run"}),
    "multiprocessing": frozenset({"Process", "Pool"}),
}
OS_SPAWN_PREFIXES = ("exec", "spawn")


@dataclass(frozen=True)
class TaxonomyInventory:
    """Collected module declarations, effective node markers and promises."""

    modules: dict[str, tuple[str, ...]]
    nodes: dict[str, tuple[str, ...]]
    promises: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class RatchetBaseline:
    """Checked-in legacy and unclassified module lists."""

    legacy_modules: frozenset[str]
    unclassified_modules: frozenset[str]


def _string_tuple_map(value: object, name: str) -> dict[str, tuple[str, ...]]:
    """Validate one JSON mapping of strings to string arrays."""
    if not isinstance(value, dict):
        raise ValueError(f"taxonomy inventory {name} must be an object")
    normalized: dict[str, tuple[str, ...]] = {}
    for key, entries in value.items():
        if not isinstance(key, str) or not isinstance(entries, list):
            raise ValueError(f"taxonomy inventory {name}[{key!r}] must map to an array")
        if not all(isinstance(entry, str) for entry in entries):
            raise ValueError(f"taxonomy inventory {name}[{key!r}] must hold strings")
        normalized[key] = tuple(entries)
    return normalized


def collect_inventory(root: Path, output: Path) -> TaxonomyInventory:
    """Collect the repository through the dedicated pytest inventory plugin."""
    env = os.environ.copy()
    env["APM_TAXONOMY_INVENTORY"] = str(output)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            INVENTORY_PLUGIN,
            "-o",
            "addopts=",
            "--strict-markers",
            "--collect-only",
            "-q",
            "tests",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout + result.stderr)
    data = json.loads(output.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"modules", "nodes", "promises"}:
        raise ValueError("taxonomy inventory has an unexpected shape")
    return TaxonomyInventory(
        modules=_string_tuple_map(data["modules"], "modules"),
        nodes=_string_tuple_map(data["nodes"], "nodes"),
        promises=_string_tuple_map(data["promises"], "promises"),
    )


def _sorted_unique_paths(value: object, name: str) -> frozenset[str]:
    """Validate a sorted, unique list of repository-relative paths."""
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item.startswith("tests/") for item in value)
        or value != sorted(set(value))
    ):
        raise BaselineError(f"invalid taxonomy ratchet baseline: {name} must be sorted paths")
    return frozenset(value)


def parse_baseline(payload: dict[str, object]) -> RatchetBaseline:
    """Validate the checked-in taxonomy ratchet baseline."""
    if set(payload) != BASELINE_KEYS or payload["schema_version"] != 1:
        raise BaselineError("invalid taxonomy ratchet baseline: unexpected object shape")
    if payload["description"] != BASELINE_DESCRIPTION:
        raise BaselineError(
            "invalid taxonomy ratchet baseline: description must match "
            "BASELINE_DESCRIPTION in tests/quality/taxonomy_ratchets.py"
        )
    legacy = _sorted_unique_paths(payload["legacy_modules"], "legacy_modules")
    unclassified = _sorted_unique_paths(payload["unclassified_modules"], "unclassified_modules")
    if not unclassified <= legacy:
        raise BaselineError(
            "invalid taxonomy ratchet baseline: unclassified_modules must be legacy modules"
        )
    return RatchetBaseline(legacy_modules=legacy, unclassified_modules=unclassified)


def load_ratchet_baseline(root: Path) -> RatchetBaseline:
    """Load and validate the checked-in taxonomy ratchet baseline."""
    return parse_baseline(load_baseline(root / BASELINE, label="taxonomy ratchet"))


def baseline_payload(legacy: set[str], unclassified: set[str]) -> dict[str, object]:
    """Return the canonical taxonomy ratchet baseline payload."""
    return {
        "description": BASELINE_DESCRIPTION,
        "legacy_modules": sorted(legacy),
        "schema_version": 1,
        "unclassified_modules": sorted(unclassified),
    }


def catalog_source_ids(root: Path) -> dict[str, frozenset[str]]:
    """Return promise ids currently defined by each catalog source."""
    spec_ids = frozenset(SPEC_ANCHOR.findall((root / SPEC).read_text(encoding="utf-8")))
    index = json.loads((root / OWNERS_DIR / "index.json").read_text(encoding="utf-8"))
    owner_ids: set[str] = set()
    for shard in index["shards"]:
        data = json.loads((root / OWNERS_DIR / shard).read_text(encoding="utf-8"))
        owner_ids.update(owner["id"] for owner in data["owners"])
    return {"architecture-owner": frozenset(owner_ids), "spec": spec_ids}


CATALOG_DESCRIPTION = (
    "Promise ids that test modules may name in pytest.mark.promise. Seeded from "
    "the OpenAPM v0.1 requirement anchors (source 'spec', <a id=\"req-...\"> in "
    "docs/src/content/docs/specs/openapm-v0.1.md) and the registered architecture "
    "owner ids (source 'architecture-owner', .apm/architecture/owners). Every id "
    "must still exist in its source. Add a new id here when its spec requirement "
    "or owner is registered."
)


def catalog_payload(sources: dict[str, frozenset[str]]) -> dict[str, object]:
    """Return the canonical promise catalog for every current source id."""
    return {
        "description": CATALOG_DESCRIPTION,
        "promises": sorted(
            (
                {"id": promise, "source": source}
                for source, ids in sources.items()
                for promise in ids
            ),
            key=lambda entry: entry["id"],
        ),
        "schema_version": 1,
    }


class MergeBaseUnavailable(RuntimeError):
    """Raised when the merge-base needed for a monotonicity check is missing."""


def merge_base_text(root: Path, relative: Path) -> str | None:
    """Return `relative` at the merge-base with the base branch, None if absent there.

    The base ref is ``APM_RATCHET_BASE_REF`` or ``origin/main``. Only the base
    copy of the file is read, so the check stays independent of history shape.
    """
    base_ref = os.environ.get("APM_RATCHET_BASE_REF", "origin/main")
    merge_base = subprocess.run(
        ["git", "-C", str(root), "merge-base", base_ref, "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if merge_base.returncode != 0:
        raise MergeBaseUnavailable(
            f"cannot find the merge-base of {base_ref} and HEAD "
            f"({merge_base.stderr.strip() or 'shallow or missing history'}); "
            "fetch full history (for example `git fetch --unshallow origin main`) "
            "or set APM_RATCHET_BASE_REF"
        )
    shown = subprocess.run(
        ["git", "-C", str(root), "show", f"{merge_base.stdout.strip()}:{relative.as_posix()}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return shown.stdout if shown.returncode == 0 else None


def baseline_growth(base: RatchetBaseline | None, current: RatchetBaseline) -> list[str]:
    """Return entries `current` added relative to the merge-base baseline."""
    if base is None:
        return []
    added = [
        (field, sorted(getattr(current, field) - getattr(base, field)))
        for field in ("legacy_modules", "unclassified_modules")
    ]
    return [
        f"{BASELINE.as_posix()}: {field} grew relative to the merge-base: {paths}. "
        "Fix: remove the added entries and classify, place or link the modules instead."
        for field, paths in added
        if paths
    ]


def catalog_problems(payload: object, sources: dict[str, frozenset[str]]) -> list[str]:
    """Return integrity problems for the promise catalog payload."""
    if not isinstance(payload, dict) or set(payload) != {
        "description",
        "promises",
        "schema_version",
    }:
        return [f"{CATALOG.as_posix()}: unexpected object shape"]
    entries = payload["promises"]
    if payload["schema_version"] != 1 or not isinstance(entries, list) or not entries:
        return [f"{CATALOG.as_posix()}: unsupported schema or empty promises"]
    problems: list[str] = []
    ids: list[str] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"id", "source"}
            or entry["source"] not in CATALOG_SOURCES
            or not isinstance(entry["id"], str)
        ):
            problems.append(f"{CATALOG.as_posix()}: malformed promise entry {entry!r}")
            continue
        ids.append(entry["id"])
        if entry["id"] not in sources[entry["source"]]:
            problems.append(
                f"{CATALOG.as_posix()}: promise id {entry['id']!r} no longer exists in its "
                f"{entry['source']} source. Fix: remove it from the catalog and from every "
                "pytest.mark.promise that names it, or restore the source id."
            )
    if ids != sorted(set(ids)):
        problems.append(f"{CATALOG.as_posix()}: promise ids must be sorted and unique")
    return problems


def load_catalog_ids(root: Path) -> frozenset[str]:
    """Return promise ids from the checked-in catalog."""
    payload = json.loads((root / CATALOG).read_text(encoding="utf-8"))
    return frozenset(entry["id"] for entry in payload["promises"])


def _behavioral(markers: tuple[str, ...]) -> tuple[str, ...]:
    """Return behavioral markers from one collected marker sequence."""
    return tuple(marker for marker in markers if marker in BEHAVIORAL_MARKERS)


def process_spawns(source: str | bytes, filename: str) -> list[str]:
    """Return 'line N: call' descriptions of process-spawning calls in a module."""
    tree = ast.parse(source, filename=filename)
    module_alias: dict[str, str] = {}
    name_alias: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in SPAWN_CALLS:
                    module_alias[alias.asname or root] = root
        elif isinstance(node, ast.ImportFrom) and node.module in SPAWN_CALLS:
            for alias in node.names:
                name_alias[alias.asname or alias.name] = (node.module, alias.name)

    found: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        target: tuple[str, str] | None = None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            module = module_alias.get(func.value.id)
            if module is not None:
                target = (module, func.attr)
        elif isinstance(func, ast.Name):
            target = name_alias.get(func.id)
        if target is None:
            continue
        module, attr = target
        if attr in SPAWN_CALLS[module] or (module == "os" and attr.startswith(OS_SPAWN_PREFIXES)):
            found.add((node.lineno, f"{module}.{attr}"))
    return [f"line {line}: {call}" for line, call in sorted(found)]


def unclassified_growth(
    inventory: TaxonomyInventory,
    baseline: RatchetBaseline,
) -> list[str]:
    """Return one diagnostic per unclassified module missing from the baseline."""
    return [
        f"{module}: unclassified test module is not in the baseline. Fix: add exactly "
        "one module-level behavioral marker, for example "
        "`pytestmark = [pytest.mark.unit]` (unit, component or e2e)."
        for module, markers in sorted(inventory.modules.items())
        if not _behavioral(markers) and module not in baseline.unclassified_modules
    ]


def placement_violations(
    inventory: TaxonomyInventory,
    baseline: RatchetBaseline,
    root: Path,
) -> list[str]:
    """Return placement diagnostics for newly added modules."""
    problems: list[str] = []
    for module, markers in sorted(inventory.modules.items()):
        if module in baseline.legacy_modules:
            continue
        if module.startswith("tests/integration/") and "unit" in _behavioral(markers):
            problems.append(
                f"{module}: new module under tests/integration is marked unit. Fix: move "
                "it to tests/unit, or mark it component or e2e if it crosses a boundary."
            )
        if module.startswith("tests/unit/"):
            spawns = process_spawns((root / module).read_bytes(), module)
            if spawns:
                problems.append(
                    f"{module}: new module under tests/unit spawns a process "
                    f"({'; '.join(spawns)}). Fix: replace the process with an in-process "
                    "call or a mock, or move the module to tests/integration and mark it "
                    "component or e2e."
                )
    return problems


def promise_violations(
    inventory: TaxonomyInventory,
    baseline: RatchetBaseline,
    catalog_ids: frozenset[str],
) -> list[str]:
    """Return promise-link diagnostics for new modules and unknown promise ids."""
    problems: list[str] = []
    for module in sorted(inventory.modules):
        ids = inventory.promises.get(module, ())
        if not ids and module not in baseline.legacy_modules:
            problems.append(
                f"{module}: new test module declares no promise. Fix: add "
                '`pytest.mark.promise("<id>", ...)` to its module-level pytestmark, '
                f"naming ids from {CATALOG.as_posix()}."
            )
            continue
        unknown = sorted(set(ids) - catalog_ids)
        if unknown:
            problems.append(
                f"{module}: pytest.mark.promise names unknown id(s) {unknown}. Fix: use "
                f"ids listed in {CATALOG.as_posix()}, or add the missing spec requirement "
                "or architecture owner id to the catalog."
            )
    return problems


def stale_entries(
    inventory: TaxonomyInventory,
    baseline: RatchetBaseline,
    root: Path,
) -> tuple[set[str], set[str]]:
    """Return legacy and unclassified baseline entries that no longer apply."""
    stale_legacy = {module for module in baseline.legacy_modules if not (root / module).is_file()}
    stale_unclassified = {
        module
        for module in baseline.unclassified_modules
        if module in stale_legacy
        or (module in inventory.modules and _behavioral(inventory.modules[module]))
    }
    return stale_legacy, stale_unclassified


def ratchet_problems(
    inventory: TaxonomyInventory,
    baseline: RatchetBaseline,
    catalog_ids: frozenset[str],
    root: Path,
) -> list[str]:
    """Return every growth diagnostic across the three ratchets."""
    return (
        unclassified_growth(inventory, baseline)
        + placement_violations(inventory, baseline, root)
        + promise_violations(inventory, baseline, catalog_ids)
    )


def main(argv: list[str] | None = None) -> int:
    """Drop stale taxonomy baseline entries; refuse when any ratchet grew."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--update-baseline", action="store_true", required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        baseline = load_ratchet_baseline(root)
        with tempfile.TemporaryDirectory() as scratch:
            inventory = collect_inventory(root, Path(scratch) / "inventory.json")
        problems = ratchet_problems(inventory, baseline, load_catalog_ids(root), root)
    except (BaselineError, OSError, RuntimeError, ValueError) as error:
        print(f"[x] {error}", file=sys.stderr)
        return 2
    if problems:
        for problem in problems:
            print(f"[x] {problem}", file=sys.stderr)
        print(
            "[x] refusing to update taxonomy ratchet baseline with new debt; "
            "fix the listed modules instead",
            file=sys.stderr,
        )
        return 1
    stale_legacy, stale_unclassified = stale_entries(inventory, baseline, root)
    if not stale_legacy and not stale_unclassified:
        print("[i] taxonomy ratchet baseline already current; no update written")
        return 0
    try:
        write_baseline(
            root / BASELINE,
            baseline_payload(
                set(baseline.legacy_modules - stale_legacy),
                set(baseline.unclassified_modules - stale_unclassified),
            ),
            label="taxonomy ratchet",
        )
    except BaselineError as error:
        print(f"[x] {error}", file=sys.stderr)
        return 2
    print(
        "[+] updated taxonomy ratchet baseline: removed "
        f"{len(stale_legacy)} legacy and {len(stale_unclassified)} unclassified entr"
        f"{'y' if len(stale_legacy) + len(stale_unclassified) == 1 else 'ies'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
