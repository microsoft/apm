"""Owner-surface PR check: the integration tests an owner or install-path change can break.

Today's main reds (the lockfile-read owner test after #4067, the codex bundle
e2e after #4007) lived in integration shards that run only on merge_group or
dispatch. This check runs, on every pull request, just the slice of those
shards that the change can reach, so it stays cheap enough to require:

* the owner-rule mutation cases and other owner-guard tests whose parameters or
  body name a changed file, an affected owner, guard or rule;
* the ``tests/integration/test_architecture_*.py`` files whose text names one;
* every ``tests/integration/test_install_*.py`` file when install-path code
  changed;
* any changed test file in those groups, whole.

The full integration suite still runs once per train head (merge_group or a
merge-train ref), never per pull request.

Usage::

    python -m scripts.owner_surface plan --base <sha> --out <file>
    python -m scripts.owner_surface run --base <sha> --changed <file> [pytest args...]

``plan`` needs only the standard library and git. It writes the changed paths
to ``--out`` and ``run=true|false`` to ``$GITHUB_OUTPUT``. ``run`` builds the
selection keys from the owner registry and rule catalog and runs pytest with
this module as a plugin; the plugin (active only while
``OWNER_SURFACE_SELECTION`` is set, so xdist workers deselect the same items)
drops every collected item that names no key.
"""

from __future__ import annotations

import fnmatch
import inspect
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

try:
    import pytest
except ImportError:  # `plan` runs on the runner's Python before the project env exists.
    pytest = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
OWNERS_DIR = ".apm/architecture/owners"
OWNERS_INDEX = f"{OWNERS_DIR}/index.json"
SELECTION_ENV = "OWNER_SURFACE_SELECTION"

ARCHITECTURE_TESTS = "tests/integration/test_architecture_*.py"
MUTATION_TESTS = "tests/integration/test_architecture_owner_rule_mutations.py"
INSTALL_TESTS = "tests/integration/test_install_*.py"
# Install-path code: a change here also runs every install integration file.
INSTALL_SURFACE = ("src/apm_cli/install/*", "src/apm_cli/deps/lockfile*")
# Owner surfaces besides every file an owner registry selector names.
SURFACE = (
    *INSTALL_SURFACE,
    ".apm/architecture/*",
    "scripts/architecture_linter/*",
    ARCHITECTURE_TESTS,
    INSTALL_TESTS,
    # The check itself: a selection change runs its own slice.
    "scripts/owner_surface.py",
    ".github/workflows/owner-surface.yml",
)
_SRC_MODULE = re.compile(r"^src/(apm_cli/.+)\.py$")


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _run_git(args: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
    # Fixed git argv from this module (a revision and repository paths), no shell.
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _git(*args: str, root: Path = ROOT) -> str:
    result = _run_git(args, root)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def changed_paths(base: str, root: Path = ROOT) -> list[str]:
    """Paths the checked-out commit changes against ``base`` (deletions included)."""
    return sorted(
        line for line in _git("diff", "--name-only", base, "HEAD", root=root).splitlines() if line
    )


def _owner_shards(read: Any) -> list[str]:
    index = json.loads(read(OWNERS_INDEX) or '{"shards": []}')
    return [f"{OWNERS_DIR}/{shard}" for shard in index.get("shards", [])]


def load_owners(read: Any) -> dict[str, dict[str, Any]]:
    """Owner entries by id, read through ``read(path) -> text | None``."""
    owners: dict[str, dict[str, Any]] = {}
    for shard in _owner_shards(read):
        text = read(shard)
        if text is None:
            continue
        for owner in json.loads(text).get("owners", []):
            owners[owner["id"]] = owner
    return owners


def _read_worktree(root: Path) -> Any:
    def read(path: str) -> str | None:
        target = root / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    return read


def _read_revision(base: str, root: Path) -> Any:
    def read(path: str) -> str | None:
        result = _run_git(("show", f"{base}:{path}"), root)
        return result.stdout if result.returncode == 0 else None

    return read


def on_surface(changed: Iterable[str], owners: Mapping[str, Mapping[str, Any]]) -> bool:
    """True when a change touches an owner surface (or a file an owner selects)."""
    selectors = [selector for owner in owners.values() for selector in owner.get("selectors", ())]
    return any(_matches(path, SURFACE) or _matches(path, selectors) for path in changed)


def affected_owners(
    changed: Iterable[str],
    current: Mapping[str, Mapping[str, Any]],
    base: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    """Owners that select a changed file, or whose registry entry changed."""
    paths = list(changed)
    hit = {
        owner_id
        for owner_id, owner in current.items()
        if any(_matches(path, owner.get("selectors", ())) for path in paths)
    }
    hit.update(owner_id for owner_id, owner in current.items() if base.get(owner_id) != owner)
    hit.update(owner_id for owner_id in base if owner_id not in current)
    return hit


def selection_keys(
    changed: Sequence[str],
    current: Mapping[str, Mapping[str, Any]],
    base: Mapping[str, Mapping[str, Any]],
    rules: Iterable[tuple[str, tuple[str, ...], str]],
) -> set[str]:
    """Text keys an affected test names: paths, modules, owner, guard and rule ids.

    ``rules`` yields ``(rule_id, guard_ids, source_path)`` for every catalog rule.
    """
    # Only code paths are keys: a test naming CHANGELOG.md is not reachable code.
    keys = {path for path in changed if path.endswith(".py")}
    for path in changed:
        module = _SRC_MODULE.match(path)
        if module:
            keys.add(module.group(1).replace("/", ".").removesuffix(".__init__"))
    owners = affected_owners(changed, current, base)
    # Both revisions: a removed or replaced guard still selects the tests that name it.
    guards = {
        guard
        for owner_id in owners
        for revision in (base, current)
        for guard in revision.get(owner_id, {}).get("guards", ())
    }
    changed_set = set(changed)
    for rule_id, guard_ids, source in rules:
        if source in changed_set:
            guards.update(guard_ids)
            keys.add(rule_id)
        if guards.intersection(guard_ids):
            keys.add(rule_id)
    owners.update(
        owner_id
        for owner_id, owner in current.items()
        if guards.intersection(owner.get("guards", ()))
    )
    return keys | owners | guards


def target_files(changed: Sequence[str], root: Path = ROOT) -> list[str]:
    """Test files to collect: every architecture file, plus install files for install changes."""
    patterns = [ARCHITECTURE_TESTS]
    if any(_matches(path, INSTALL_SURFACE) for path in changed):
        patterns.append(INSTALL_TESTS)
    found = {
        path.relative_to(root).as_posix()
        for pattern in patterns
        for path in root.glob(pattern)
        if path.is_file()
    }
    # A changed test in either group always runs, even without a source change.
    found.update(
        path
        for path in changed
        if _matches(path, (ARCHITECTURE_TESTS, INSTALL_TESTS)) and (root / path).is_file()
    )
    return sorted(found)


def _rule_catalog(root: Path) -> list[tuple[str, tuple[str, ...], str]]:
    from scripts.architecture_linter.runner import registered_rules

    catalog = []
    for rule in registered_rules():
        source = inspect.getsourcefile(rule.check) or ""
        try:
            relative = Path(source).resolve().relative_to(root).as_posix()
        except ValueError:
            relative = source
        catalog.append((rule.id, tuple(rule.guard_ids), relative))
    return catalog


def build_selection(base: str, changed: Sequence[str], root: Path = ROOT) -> dict[str, Any]:
    current = load_owners(_read_worktree(root))
    previous = load_owners(_read_revision(base, root))
    keys = selection_keys(changed, current, previous, _rule_catalog(root))
    install = any(_matches(path, INSTALL_SURFACE) for path in changed)
    return {
        "keys": sorted(keys),
        "changed": sorted(changed),
        "install": install,
        "targets": target_files(changed, root),
    }


# -- pytest plugin -----------------------------------------------------------


def _text(value: Any) -> str:
    if is_dataclass(value) and not isinstance(value, type):
        return " ".join(_text(getattr(value, item.name)) for item in fields(value))
    if isinstance(value, Mapping):
        return " ".join(f"{_text(k)} {_text(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(_text(item) for item in value)
    return str(value)


def _names_a_key(text: str, keys: Iterable[str]) -> bool:
    return any(key in text for key in keys)


def _function_text(function: Any) -> str:
    """A test's source plus the module-level string constants it references (a rule-id constant)."""
    source = inspect.getsource(function)
    namespace = getattr(function, "__globals__", {})
    code = getattr(function, "__code__", None)
    names = code.co_names if code is not None else ()
    constants = [
        _text(namespace[name])
        for name in names
        if isinstance(namespace.get(name), (str, tuple, frozenset))
    ]
    return "\n".join([source, *constants])


def keep_item(item: Any, selection: Mapping[str, Any], file_text: Any) -> bool:
    """Whether one collected item is in the owner-surface slice."""
    path = Path(str(item.path)).resolve().relative_to(ROOT).as_posix()
    keys = selection["keys"]
    if path in selection["changed"]:
        return True
    if selection["install"] and fnmatch.fnmatchcase(path, INSTALL_TESTS):
        return True
    if path != MUTATION_TESTS:
        return _names_a_key(file_text(path), keys)
    callspec = getattr(item, "callspec", None)
    if callspec is not None and _names_a_key(_text(callspec.params), keys):
        return True
    try:
        source = _function_text(item.function)
    except (OSError, TypeError):
        # Unreadable source: a parametrized case already matched on its params.
        return callspec is None
    if _names_a_key(source, keys):
        return True
    # Unparametrized structural checks (coverage, ordering, a clean baseline)
    # stay; anything else that runs a rule names no key, so it is deselected.
    return callspec is None and "run_selected_rules" not in source


def _tryfirst(function: Any) -> Any:
    # Deselect before scripts.ci_sharding (trylast) partitions what remains.
    return pytest.hookimpl(tryfirst=True)(function) if pytest is not None else function


def shard_items(items: list[Any], index: int, count: int, durations: str | None) -> list[Any]:
    """Shard ``index`` of ``count`` of the slice; a slice too small to split leaves shards empty.

    scripts.ci_sharding refuses empty shards, so the slice is split into as many
    nonempty, group-preserving shards as it allows (at most ``count``) and the
    surplus shards run nothing. Every worker computes the same split.
    """
    from scripts.ci_sharding import load_durations, partition

    seconds = load_durations(durations)
    for usable in range(min(count, len(items)), 0, -1):
        try:
            chosen = partition(items, usable, seconds)
        except pytest.UsageError:
            continue
        return list(chosen.shards[index - 1]) if index <= usable else []
    return []


@_tryfirst
def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    location = os.environ.get(SELECTION_ENV)
    if not location:
        return
    selection = json.loads(Path(location).read_text(encoding="utf-8"))
    cache: dict[str, str] = {}

    def file_text(path: str) -> str:
        if path not in cache:
            cache[path] = (ROOT / path).read_text(encoding="utf-8")
        return cache[path]

    kept, dropped = [], []
    for item in items:
        (kept if keep_item(item, selection, file_text) else dropped).append(item)
    shard = selection.get("shard")
    if shard:
        mine = {id(item) for item in shard_items(kept, *shard, selection.get("durations"))}
        dropped.extend(item for item in kept if id(item) not in mine)
        kept = [item for item in kept if id(item) in mine]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept


# -- CLI ---------------------------------------------------------------------


def _write_output(name: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def plan(base: str, out: Path, root: Path = ROOT) -> bool:
    changed = changed_paths(base, root)
    out.write_text("".join(f"{path}\n" for path in changed), encoding="utf-8")
    run = on_surface(changed, load_owners(_read_worktree(root)))
    _write_output("run", "true" if run else "false")
    print(f"{len(changed)} changed file(s); owner surface touched: {run}")
    return run


def run(
    base: str,
    changed_file: Path,
    extra: Sequence[str],
    root: Path = ROOT,
    shard: tuple[int, int] | None = None,
    durations: str | None = None,
) -> int:
    changed = [line for line in changed_file.read_text(encoding="utf-8").splitlines() if line]
    selection = build_selection(base, changed, root)
    selection["shard"] = list(shard) if shard else None
    selection["durations"] = durations
    location = root / "build" / "owner-surface-selection.json"
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_text(json.dumps(selection, indent=2), encoding="utf-8")
    print(f"owner-surface keys ({len(selection['keys'])}): {', '.join(selection['keys'])}")
    os.environ[SELECTION_ENV] = str(location)
    code = int(pytest.main([*selection["targets"], "-p", "scripts.owner_surface", *extra]))
    # Nothing selected is a pass: no reachable owner test.
    return 0 if code == int(pytest.ExitCode.NO_TESTS_COLLECTED) else code


def _shard(value: str) -> tuple[int, int]:
    index, _, count = value.partition("/")
    if not (index.isdigit() and count.isdigit() and 1 <= int(index) <= int(count)):
        raise ValueError(f"--shard must be INDEX/COUNT with 1 <= INDEX <= COUNT, got {value!r}")
    return int(index), int(count)


def main(argv: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m scripts.owner_surface")
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan", help="list changed files; decide whether to run")
    plan_parser.add_argument("--base", required=True)
    plan_parser.add_argument("--out", required=True, type=Path)
    run_parser = commands.add_parser("run", help="run the owner-surface slice through pytest")
    run_parser.add_argument("--base", required=True)
    run_parser.add_argument("--changed", required=True, type=Path)
    run_parser.add_argument("--shard", type=_shard, help="INDEX/COUNT, e.g. 1/3")
    run_parser.add_argument("--durations", help="JSON {node_id: seconds} that balances shards")
    args, extra = parser.parse_known_args(argv)
    if extra[:1] == ["--"]:
        extra = extra[1:]
    if args.command == "plan":
        if extra:
            parser.error(f"unexpected arguments: {extra}")
        plan(args.base, args.out)
        return 0
    return run(args.base, args.changed, extra, shard=args.shard, durations=args.durations)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
