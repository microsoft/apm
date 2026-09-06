"""Small source-derived routing expectations and open-world transition assertions."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TypeVar

from apm_cli.deps.lockfile import LockFile
from apm_cli.integration.targets import KNOWN_TARGETS
from tests.utils.artifact_snapshot import (
    ArtifactSnapshotSet,
    assert_snapshot_changes_within,
    assert_snapshot_set_unchanged,
)
from tests.utils.lifecycle_interactions import RoutingRow

T = TypeVar("T")


def ancestors(paths: Iterable[str]) -> frozenset[str]:
    """Permit directory entries, never all descendants of an ancestor."""
    return frozenset(
        parent.as_posix()
        for path in paths
        for parent in PurePosixPath(path).parents
        if parent.as_posix() != "."
    )


@dataclass(frozen=True)
class SourceFixture:
    """Authored input identity; no field comes from a product output ledger."""

    package_name: str
    primitive: str
    name: str
    marker: str
    source_files: tuple[str, ...]


@dataclass(frozen=True)
class RoutingExpectation:
    """Exact native files and their independently expected ledger owners."""

    files: frozenset[str]
    ledger: frozenset[tuple[str, str]]
    shared: frozenset[str] = frozenset()


def expected_routing(
    row: RoutingRow,
    sources: tuple[SourceFixture, ...],
    targets: tuple[str, ...] | None = None,
) -> RoutingExpectation:
    """Map the tiny fixture layout through catalog capabilities, not integrators."""
    files: set[str] = set()
    ledger: set[tuple[str, str]] = set()
    shared: set[str] = set()
    for target in row.targets if targets is None else targets:
        profile = KNOWN_TARGETS[target].for_scope(user_scope=row.user_scope)
        assert profile is not None
        for source in sources:
            kind = source.primitive
            mapping = profile.primitives.get(kind)
            # A prompt fixture can also be rendered as a command on widening.
            if mapping is None and kind == "prompts":
                mapping = profile.primitives.get("commands")
            if mapping is None:
                continue
            base = PurePosixPath(mapping.deploy_root or profile.root_dir) / mapping.subdir
            owned: set[str]
            if kind == "skills":
                owned = {(base / source.name / "SKILL.md").as_posix()}
                ledger.add((profile.name, (base / source.name).as_posix()))
            elif kind == "canvas":
                owned = {
                    (base / source.name / suffix).as_posix()
                    for suffix in ("extension.mjs", "assets/info.txt")
                }
            elif mapping.format_id == "copilot_user_instructions":
                owned = {(base / "copilot-instructions.md").as_posix()}
                shared.update(owned)
            elif mapping.format_id == "grok_rules":
                owned = {(base / f"{source.name}.instructions.md").as_posix()}
            elif kind == "hooks" and profile.hooks_config_display:
                config = (
                    PurePosixPath(profile.root_dir)
                    / PurePosixPath(profile.hooks_config_display).name
                ).as_posix()
                sidecar = (PurePosixPath(profile.root_dir) / "apm-hooks.json").as_posix()
                files.update((config, sidecar))
                shared.update((config, sidecar))
                continue
            elif kind == "hooks":
                suffix = "-pretooluse-1" if mapping.format_id == "kiro_hooks" else ""
                owned = {(base / f"{source.package_name}-{source.name}{suffix}.json").as_posix()}
            else:
                owned = {(base / f"{source.name}{mapping.extension}").as_posix()}
            files.update(owned)
            ledger.update((profile.name, path) for path in owned)
    return RoutingExpectation(frozenset(files), frozenset(ledger), frozenset(shared))


def assert_members_preserved(expected: object, actual: object, *, path: str = "$") -> None:
    """Protect foreign nested configuration values inside a writable shared file."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"Shared configuration replaced at {path}"
        for key, value in expected.items():
            assert key in actual, f"Shared configuration member removed: {path}.{key}"
            assert_members_preserved(value, actual[key], path=f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"Shared configuration array replaced at {path}"
        assert all(item in actual for item in expected), f"Shared ownership lost at {path}"
    else:
        assert actual == expected, f"Shared configuration member overwritten: {path}"


@dataclass
class InteractionOracle:
    """One mandatory observation boundary with lifetime native-path ownership."""

    roots: Mapping[str, Path]
    deployment_root_id: str
    lock_root: Path
    sources: tuple[SourceFixture, ...]
    row: RoutingRow
    protected_json: dict[Path, object] = field(default_factory=dict)
    introduced: set[str] = field(default_factory=set)
    operations: list[str] = field(default_factory=list)
    evaluations: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    pending: str | None = None

    def capture(self) -> ArtifactSnapshotSet:
        """Observe complete roots, including unrelated target neighbors."""
        return ArtifactSnapshotSet.capture(self.roots)

    def observe(
        self,
        operation: str,
        action: Callable[[], T],
        *,
        exact: Mapping[str, Iterable[str]] | None = None,
        trees: Mapping[str, Iterable[str]] | None = None,
        unchanged: bool = False,
    ) -> T:
        """Observe every CLI/fixture action and fail closed on forgotten evaluation."""
        with self.transition(operation, exact=exact, trees=trees, unchanged=unchanged):
            return action()

    @contextmanager
    def transition(
        self,
        operation: str,
        *,
        exact: Mapping[str, Iterable[str]] | None = None,
        trees: Mapping[str, Iterable[str]] | None = None,
        unchanged: bool = False,
    ) -> Iterator[None]:
        """Observe compound fixture setup with the same mandatory CLI boundary."""
        assert self.pending is None, f"Skipped transition assertions: {self.pending}"
        before = self.capture()
        try:
            yield
        finally:
            after = self.capture()
        if unchanged:
            assert_snapshot_set_unchanged(before, after)
        else:
            exact = exact or {}
            trees = trees or {}
            allowed = {
                root_id: frozenset(exact.get(root_id, ()))
                | ancestors((*exact.get(root_id, ()), *trees.get(root_id, ())))
                for root_id in self.roots
            }
            assert_snapshot_changes_within(before, after, exact_paths=allowed, tree_prefixes=trees)
            for root_id, snapshot in after.snapshots:
                directories = ancestors((*exact.get(root_id, ()), *trees.get(root_id, ())))
                assert all(
                    entry.kind == "directory"
                    for entry in snapshot.entries
                    if entry.relative_path in directories
                ), f"Writable ancestor is not a directory: {root_id}"
        for path, expected in self.protected_json.items():
            assert path.is_file(), f"Shared configuration deleted: {path}"
            assert_members_preserved(expected, json.loads(path.read_text(encoding="utf-8")))
        self.operations.append(operation)
        self.pending = operation

    def evaluated(self, *laws: str) -> None:
        """Record laws only after their corresponding assertions returned."""
        assert self.pending is not None, "Evaluation without an observed transition"
        self.evaluations.append(
            (
                self.pending,
                ("filesystem.open_world_observation", "ownership.preserve_unowned", *laws),
            )
        )
        self.pending = None

    def assert_routing(self, targets: tuple[str, ...]) -> None:
        """Require every expected file/claim and reject extra claims and stale routes."""
        expected = expected_routing(self.row, self.sources, targets)
        root = self.roots[self.deployment_root_id]
        for name in expected.files:
            path = root / name
            assert path.is_file() and path.read_bytes(), f"Missing required deployment: {name}"
        lock = LockFile.read(self.lock_root / "apm.lock.yaml")
        records = () if lock is None else lock.deployment_ledger.records.values()
        observed = {(record.locator.target, record.locator.value) for record in records}
        assert observed == set(expected.ledger), (
            f"Ledger differs from source-derived routing: missing={set(expected.ledger) - observed}, "
            f"unexpected={observed - set(expected.ledger)}"
        )
        active_paths = set(expected.files) | {path for _target, path in expected.ledger}
        possible_paths: set[str] = set(self.introduced)
        possible_shared: set[str] = set(expected.shared)
        for name, base in KNOWN_TARGETS.items():
            if (
                base.user_root_resolver is not None
                or base.for_scope(user_scope=self.row.user_scope) is None
            ):
                continue
            alternative = expected_routing(self.row, self.sources, (name,))
            possible_paths.update(alternative.files)
            possible_shared.update(alternative.shared)
        # Include never-authorized and previously-widened outputs, not just initial claims.
        for name in possible_paths - active_paths:
            path = root / name
            if name in possible_shared and path.exists():
                contents = path.read_text(encoding="utf-8")
                assert all(
                    source.package_name not in contents and source.marker not in contents
                    for source in self.sources
                ), f"Removed target leaked shared ownership: {name}"
            else:
                assert not path.exists(), f"Removed target leaked deployment: {name}"
        if targets:
            assert lock is not None, "Successful materialization omitted lockfile"
            for source in self.sources:
                source_paths = expected_routing(self.row, (source,), targets).files
                assert source_paths, f"Source has no authorized route: {source.package_name}"
                for name in source_paths:
                    contents = (root / name).read_text(encoding="utf-8")
                    marker = (
                        source.package_name if name.endswith("apm-hooks.json") else source.marker
                    )
                    assert marker in contents, (
                        f"Required source content not deployed: {source.package_name}: {name}"
                    )
                    if source.marker.endswith("version-b") and not name.endswith("apm-hooks.json"):
                        assert (
                            source.marker.removesuffix("version-b") + "version-a" not in contents
                        ), f"Stale ref content survived update: {name}"
        self.introduced.update(active_paths)

    def assert_finished(self, required_operations: Iterable[str]) -> None:
        """Reject skipped intermediate assertions or absent required transitions."""
        assert self.pending is None, f"Skipped transition assertions: {self.pending}"
        missing = set(required_operations) - {name for name, _laws in self.evaluations}
        assert not missing, f"Missing evaluated transitions: {sorted(missing)}"
