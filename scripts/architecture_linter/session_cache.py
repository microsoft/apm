"""Session-scoped reuse of the linter's inventory, owner registry and file facts.

Every run obtains its :class:`LinterSession` from :func:`current_session`.
Outside :func:`linter_session` that is a fresh, run-private session, so a
single run behaves exactly as before. Inside :func:`linter_session` (the
mutation-test matrix), consecutive runs share one session:

* the inventory is walked once per repository root -- the tree must not
  change while the session is open; per-case edits go through
  ``source_overrides`` instead;
* the owner registry is validated once per exact document and inventory
  snapshot;
* file facts (one read, one parse, one AST walk) are memoized by
  ``(path, sha256(source), sha256(linter source), collectors)``. Overrides are read on top of the
  session, so an overridden file hashes differently and is rebuilt while
  every untouched file reuses its single parse.

Every key also carries the linter's own source fingerprint (rules, facts,
runner: every ``.py`` under ``scripts/architecture_linter``), taken when the
session opens, so an edited rule or extractor never reuses stale facts.

``fact_builds`` counts builds per content key: a value above one means a file
was parsed twice for the same content, which the session forbids.
"""

from __future__ import annotations

import hashlib
import pickle
import tempfile
from collections import Counter
from collections.abc import Callable, Hashable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import TypeVar

from scripts.architecture_linter.inventory import Inventory, build_inventory
from scripts.architecture_linter.models import FileFacts

FactsKey = tuple[str, str, str, int]
_LINTER_PACKAGE = Path(__file__).resolve().parent
_T = TypeVar("_T")
_FACTS_PAYLOAD_LENGTH = 14

_ACTIVE: LinterSession | None = None


def content_digest(text: str) -> str:
    """Return the stable content key for one decoded source text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def linter_source_digest() -> str:
    """Fingerprint every linter source file (rules, facts, runner) in path order."""
    digest = hashlib.sha256()
    for path in sorted(_LINTER_PACKAGE.rglob("*.py")):
        digest.update(path.relative_to(_LINTER_PACKAGE).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _facts_payload(facts: FileFacts) -> tuple[object, ...]:
    return (
        facts.path,
        facts.exists,
        facts.is_python,
        facts.read_error,
        facts.parse_error,
        facts.lines,
        facts.definitions,
        facts.imports,
        facts.calls,
        facts.assignments,
        facts.literals,
        facts.tree_index,
        dict(facts.extra),
        facts.visits,
    )


def _facts_from_payload(payload: object, name: str) -> FileFacts:
    if not isinstance(payload, tuple) or len(payload) != _FACTS_PAYLOAD_LENGTH:
        raise RuntimeError(f"malformed architecture fact spill: {name}")
    return FileFacts(
        path=payload[0],
        exists=payload[1],
        is_python=payload[2],
        read_error=payload[3],
        parse_error=payload[4],
        lines=payload[5],
        definitions=payload[6],
        imports=payload[7],
        calls=payload[8],
        assignments=payload[9],
        literals=payload[10],
        tree_index=payload[11],
        extra=MappingProxyType(payload[12]),
        visits=payload[13],
    )


class LinterSession:
    """One cache scope for inventories, registries and content-keyed facts."""

    def __init__(self) -> None:
        self.linter_digest = linter_source_digest()
        self._inventories: dict[Path, Inventory] = {}
        self._registries: dict[Hashable, object] = {}
        self._collector_sets: list[tuple[object, ...]] = []
        self._hot_facts: dict[FactsKey, FileFacts] = {}
        self._cold_facts: dict[FactsKey, Path] = {}
        self._spill_directory: tempfile.TemporaryDirectory[str] | None = None
        self._next_spill_id = 0
        self.fact_builds: Counter[FactsKey] = Counter()
        self.fact_hits = 0
        self.fact_misses = 0
        self.inventory_builds = 0
        self.inventory_hits = 0
        self.registry_builds = 0
        self.registry_hits = 0

    def inventory(self, root: Path) -> Inventory:
        """Return the inventory of `root`, walking it once per session."""
        key = root.resolve()
        cached = self._inventories.get(key)
        if cached is not None:
            self.inventory_hits += 1
            return cached
        inventory = build_inventory(root)
        self.inventory_builds += 1
        self._inventories[key] = inventory
        return inventory

    def registry(self, key: Hashable, build: Callable[[], _T]) -> _T:
        """Return the memoized registry outcome for an exact snapshot `key`."""
        key = (self.linter_digest, key)
        if key in self._registries:
            self.registry_hits += 1
            return self._registries[key]  # type: ignore[return-value]
        outcome = build()
        self.registry_builds += 1
        self._registries[key] = outcome
        return outcome

    def facts_key(self, relative_path: str, text: str, collectors: Sequence[object]) -> FactsKey:
        """Key facts by path, source and linter content hashes, and the collector set."""
        token = self._collector_token(tuple(collectors))
        return (relative_path, content_digest(text), self.linter_digest, token)

    def _collector_token(self, collector_set: tuple[object, ...]) -> int:
        """Number each distinct collector set by identity, never by value."""
        for token, known in enumerate(self._collector_sets):
            if len(known) == len(collector_set) and all(
                left is right for left, right in zip(known, collector_set, strict=True)
            ):
                return token
        self._collector_sets.append(collector_set)
        return len(self._collector_sets) - 1

    def lookup_facts(self, key: FactsKey) -> FileFacts | None:
        """Return previously built facts for `key`, or ``None`` on a miss."""
        facts = self._hot_facts.get(key)
        if facts is None:
            spill_path = self._cold_facts.get(key)
            if spill_path is not None:
                facts = self.load_cold(key)
        if facts is None:
            self.fact_misses += 1
        else:
            self.fact_hits += 1
        return facts

    def record_build(self, key: FactsKey) -> None:
        """Count one read-parse-walk build of the content identified by `key`."""
        self.fact_builds[key] += 1

    @property
    def max_builds_per_content(self) -> int:
        """Highest number of builds observed for one path and content hash."""
        return max(self.fact_builds.values(), default=0)

    def store_hot(self, key: FactsKey, facts: FileFacts) -> None:
        self._hot_facts[key] = facts

    def store_cold(self, key: FactsKey, facts: FileFacts) -> None:
        """Persist cold facts to this session's private spill directory."""
        if self._spill_directory is None:
            self._spill_directory = tempfile.TemporaryDirectory(prefix="apm-architecture-facts-")
        spill_path = Path(self._spill_directory.name) / f"{self._next_spill_id}.pickle"
        self._next_spill_id += 1
        with spill_path.open("wb") as handle:
            pickle.dump(_facts_payload(facts), handle, protocol=pickle.HIGHEST_PROTOCOL)
        self._cold_facts[key] = spill_path

    def has_cold(self, key: FactsKey) -> bool:
        return key in self._cold_facts

    def load_cold(self, key: FactsKey) -> FileFacts:
        """Load cold facts written by this process to its private spill folder."""
        spill_path = self._cold_facts[key]
        with spill_path.open("rb") as handle:
            payload = pickle.load(handle)  # noqa: S301 - private same-process cache
        return _facts_from_payload(payload, spill_path.name)


def current_session() -> LinterSession:
    """Return the open session, or a fresh run-private one."""
    return _ACTIVE if _ACTIVE is not None else LinterSession()


@contextmanager
def linter_session() -> Iterator[LinterSession]:
    """Share one :class:`LinterSession` across every run inside the block."""
    global _ACTIVE
    previous = _ACTIVE
    session = LinterSession()
    _ACTIVE = session
    try:
        yield session
    finally:
        _ACTIVE = previous
