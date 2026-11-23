"""Per-dependency subset field parsers and the selection-merge owner.

One dependency node can be declared by several edges (the project's
``apm.yml`` and any number of parent packages). Each edge may narrow the node
with ``skills:`` and ``targets:``. :func:`merge_edge_selections` is the single
owner of the selection the node carries: the union over edges of each edge's
``skills x targets`` product (:func:`merge_edge_pairings`), in which an
unrestricted axis (``None``) absorbs every narrowing for that edge. The
canonical result never depends on traversal order (#3735, #3763,
OpenAPM req-rs-018).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from apm_cli.core.remediation import SUBSET_PAIRINGS_INCONSISTENT
from apm_cli.utils.path_security import validate_path_segments

# Typed hook for #3203: exclude-mode selections will join this vocabulary.
# Merging any mode other than "include" must fail clearly rather than invent
# a precedence between include and exclude lists.
SelectionMode = Literal["include"]

ROOT_MANIFEST_LABEL = "apm.yml"


class SelectionMergeError(ValueError):
    """Raised when edge selections cannot be merged without inventing precedence."""


@dataclass(frozen=True)
class EdgeSelection:
    """The selection one declaring edge places on a dependency node.

    Diagnostics-only provenance: it is never persisted. ``parent_chain`` names
    the declaring packages from the shallowest down to the direct parent; an
    empty chain is the project's own ``apm.yml``. ``None`` subsets mean
    "unrestricted".
    """

    parent_chain: tuple[str, ...]
    skill_subset: tuple[str, ...] | None
    target_subset: tuple[str, ...] | None
    alias: str | None = None
    allow_insecure: bool = False
    mode: SelectionMode = "include"

    @classmethod
    def from_dependency(cls, dep_ref: Any, parent_chain: Iterable[str] = ()) -> EdgeSelection:
        """Capture the per-edge intent declared on *dep_ref*."""
        return cls(
            parent_chain=tuple(parent_chain),
            skill_subset=_normalized(getattr(dep_ref, "skill_subset", None)),
            target_subset=tuple(canonical_target_subset(getattr(dep_ref, "target_subset", None)))
            or None,
            alias=getattr(dep_ref, "alias", None),
            allow_insecure=bool(getattr(dep_ref, "allow_insecure", False)),
        )

    def declared_by(self) -> str:
        """Return a printable name for the manifest that declared this edge."""
        return " > ".join(self.parent_chain) if self.parent_chain else ROOT_MANIFEST_LABEL


@dataclass(frozen=True)
class NodeSelection:
    """The merged selection one dependency node carries.

    ``pairing`` is the exact per-target skill selection (req-rs-018);
    ``skill_subset`` and ``target_subset`` are its flat projections. An edge
    with an explicitly empty axis selects nothing and contributes no pairs;
    when every edge is explicitly empty, ``pairing`` is ``None`` and the flat
    fields keep the explicitly empty selection. ``contributors`` keeps every
    edge record, in discovery order, for diagnostics; it is never persisted.
    """

    pairing: SkillTargetSelection | None
    contributors: tuple[EdgeSelection, ...]

    @property
    def skill_subset(self) -> tuple[str, ...] | None:
        """Union of selected skills (``None`` = all)."""
        if self.pairing is None:
            return _merge_field(edge.skill_subset for edge in self.contributors)
        return self.pairing.skill_subset

    @property
    def target_subset(self) -> tuple[str, ...] | None:
        """Union of selected targets (``None`` = every effective target)."""
        if self.pairing is None:
            return _merge_field(edge.target_subset for edge in self.contributors)
        return self.pairing.target_subset

    @property
    def subset_pairings(self) -> SkillTargetSelection | None:
        """The exact pairing when the flat product would over-select, else ``None``."""
        if self.pairing is None or self.pairing.is_product:
            return None
        return self.pairing

    def lock_selection(self) -> SkillTargetSelection:
        """Return the selection a lock entry records for this node."""
        if self.pairing is not None:
            return self.pairing
        return SkillTargetSelection.from_lock_fields(self.skill_subset, self.target_subset)

    @property
    def alias(self) -> str | None:
        """Deterministic alias winner: the first (shallowest, then declared) edge."""
        return self.contributors[0].alias if self.contributors else None

    @property
    def divergent_aliases(self) -> tuple[str, ...]:
        """Declared aliases the winning alias does not honor, in discovery order."""
        winner = self.alias
        ignored: list[str] = []
        for edge in self.contributors:
            if edge.alias is not None and edge.alias != winner and edge.alias not in ignored:
                ignored.append(edge.alias)
        return tuple(ignored)

    @property
    def allow_insecure(self) -> bool:
        """An HTTP opt-in holds only when every declaring edge states it."""
        return bool(self.contributors) and all(e.allow_insecure for e in self.contributors)


def union_selection_names(*subsets: Iterable[str] | None) -> tuple[str, ...] | None:
    """Return the sorted union of the named entries, or ``None`` when empty.

    The additive core shared by edge merging and the CLI ``--skill`` overlay.
    ``None`` and empty inputs contribute no names; callers that treat ``None``
    as "unrestricted" use :func:`merge_edge_selections`.
    """
    names: set[str] = set()
    for subset in subsets:
        if subset:
            names.update(str(name) for name in subset)
    return tuple(sorted(names)) or None


def merge_edge_selections(edges: Iterable[EdgeSelection]) -> NodeSelection:
    """Merge every declaring edge's selection into the node's selection.

    The union over edges of each edge's ``skills x targets`` product, where an
    unrestricted axis (``None``) absorbs for that edge. The canonical result is
    commutative, associative and idempotent (req-rs-018).
    """
    contributors = tuple(edges)
    if not contributors:
        raise SelectionMergeError("A dependency node needs at least one declaring edge")
    unsupported = sorted({edge.mode for edge in contributors} - {"include"})
    if unsupported:
        raise SelectionMergeError(
            f"Cannot merge {', '.join(unsupported)} selections with other declarations"
        )
    # An explicitly empty axis (``()``) selects nothing: that edge pairs no
    # skill with any target, unlike ``None``, which selects all.
    selecting = [
        (edge.skill_subset, edge.target_subset)
        for edge in contributors
        if edge.skill_subset != () and edge.target_subset != ()
    ]
    return NodeSelection(
        pairing=merge_edge_pairings(selecting) if selecting else None,
        contributors=contributors,
    )


def _merge_field(values: Iterable[tuple[str, ...] | None]) -> tuple[str, ...] | None:
    collected = list(values)
    if any(value is None for value in collected):
        return None
    # An explicitly empty selection stays empty; it never widens to "all".
    return tuple(sorted({str(name) for value in collected for name in value}))


def _normalized(subset: Iterable[object] | None) -> tuple[str, ...] | None:
    if not subset:
        return None
    return tuple(sorted({str(name) for name in subset}))


def canonical_target_name(name: str) -> str:
    """Return the canonical target identifier for a recorded or declared name.

    Aliases resolve to their target (``vscode`` and ``agents`` are
    ``copilot``). Names the catalog does not know are kept as written, so a
    lock recorded by another release still reads; parsing rejects them.
    """
    from apm_cli.core.target_catalog import normalize_target_name

    try:
        return normalize_target_name(name)
    except KeyError:
        return name


def canonical_target_subset(names: Iterable[object] | None) -> list[str]:
    """Return sorted, deduplicated canonical target identifiers (req-rs-018)."""
    return sorted({canonical_target_name(str(name).strip().lower()) for name in names or ()})


def skill_subset_filter_tokens(skill_subset: Iterable[object] | None) -> set[str] | None:
    """Return match tokens for declared and flattened skill subset names.

    Skill selection accepts source-relative names such as
    ``productivity/grill-me``, while deployment promotes each selected skill
    under its leaf name (``grill-me``). Consumers of deployed skill names must
    use these tokens so install and pack apply the same matching rule.
    """
    if not skill_subset:
        return None

    tokens: set[str] = set()
    for skill_name in skill_subset:
        raw_name = str(skill_name).strip()
        if not raw_name:
            continue
        normalized_path = raw_name.replace("\\", "/")
        leaf_name = PurePosixPath(normalized_path).name
        tokens.add(raw_name)
        tokens.add(normalized_path)
        if leaf_name:
            tokens.add(leaf_name)
    return tokens or None


def parse_skill_subset(skills_raw: object) -> list[str]:
    """Validate and normalize object-form dependency ``skills:``."""
    if not isinstance(skills_raw, list):
        raise ValueError("'skills' field must be a list of skill names")
    if not skills_raw:
        raise ValueError(
            "skills: must contain at least one name; "
            "remove the field to install all skills in the bundle."
        )

    seen: set[str] = set()
    validated: list[str] = []
    for name in skills_raw:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Each entry in 'skills' must be a non-empty string")
        name = name.strip()
        validate_path_segments(name, context="skills/<name>")
        if name not in seen:
            seen.add(name)
            validated.append(name)
    return sorted(validated)


def parse_target_subset(targets_raw: object) -> list[str]:
    """Validate and normalize object-form dependency ``targets:``."""
    from apm_cli.integration.targets import KNOWN_TARGETS

    if not isinstance(targets_raw, list):
        raise ValueError("'targets' field must be a list of target names")
    if not targets_raw:
        raise ValueError(
            "targets: must contain at least one target; "
            "remove the field to route primitives to all active install targets."
        )

    valid_targets = sorted(KNOWN_TARGETS.keys())
    valid_set = set(valid_targets)
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_name in targets_raw:
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("Each entry in 'targets' must be a non-empty string")
        name = raw_name.strip().lower()
        # Aliases record as their canonical target: ``vscode`` is ``copilot``.
        if canonical_target_name(name) in valid_set:
            name = canonical_target_name(name)
        if name not in valid_set:
            suggestion = _closest_target(name, valid_targets)
            hint = f" Did you mean '{suggestion}'?" if suggestion else ""
            raise ValueError(
                f"Unknown target '{name}'. Valid targets: {', '.join(valid_targets)}.{hint}"
            )
        if name not in seen:
            seen.add(name)
            normalized.append(name)
    return sorted(normalized)


def _closest_target(value: str, valid_targets: list[str]) -> str | None:
    """Return the nearest valid target when it is within edit distance 2."""
    best: tuple[int, str] | None = None
    for target in valid_targets:
        distance = _levenshtein_distance(value, target)
        if distance <= 2 and (best is None or distance < best[0]):
            best = (distance, target)
    return best[1] if best else None


def _levenshtein_distance(left: str, right: str) -> int:
    """Return the Levenshtein edit distance between two strings."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for i, l_char in enumerate(left, start=1):
        current = [i]
        for j, r_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (l_char != r_char),
                )
            )
        previous = current
    return previous[-1]


# ---------------------------------------------------------------------------
# Per-edge skills x targets pairing (OpenAPM req-rs-018)
#
# Several dependency edges (manifest entries) can reach one dependency node.
# Each edge selects ``skills x targets``, where ``None`` on an axis means
# "all". The node selection is the union over edges of those products, so a
# skill reaches a target only when one edge pairs them. The canonical form
# below is a pure function of that meaning, which makes the merge
# commutative, associative, and idempotent (order-independent).
# ---------------------------------------------------------------------------

SUBSET_PAIRINGS_LOCK_FIELD = "subset_pairings"
_PAIRING_RECORD_KEYS = frozenset({"skills", "targets"})
_EVERY_TARGET = None

_SkillSet = frozenset[str] | None
_RawPairing = dict[str | None, _SkillSet]


@dataclass(frozen=True)
class SubsetPairing:
    """One canonical ``skills x targets`` product; ``None`` on an axis means all."""

    skills: tuple[str, ...] | None
    targets: tuple[str, ...] | None

    def to_lock_record(self) -> dict[str, list[str]]:
        """Return the lockfile mapping; an omitted axis means all."""
        record: dict[str, list[str]] = {}
        if self.skills is not None:
            record["skills"] = list(self.skills)
        if self.targets is not None:
            record["targets"] = list(self.targets)
        return record


@dataclass(frozen=True)
class SkillTargetSelection:
    """Canonical per-target skill selection of one dependency node.

    Build instances with :func:`merge_edge_pairings` or
    :meth:`from_lock_fields`; direct construction is validated to be
    canonical so equal selections always compare (and serialize) equal.
    """

    pairings: tuple[SubsetPairing, ...]

    def __post_init__(self) -> None:
        if not self.pairings:
            raise ValueError("A dependency selection needs at least one pairing")
        for pairing in self.pairings:
            for axis, values in (("skills", pairing.skills), ("targets", pairing.targets)):
                if values is not None and (
                    not isinstance(values, tuple) or _normalize_axis(values, axis) != values
                ):
                    raise ValueError(f"Subset pairing {axis} must be None or non-empty names")
        if _canonicalize(_raw_from_pairings(self.pairings)) != self.pairings:
            raise ValueError("Subset pairings are not in canonical form")

    @property
    def skill_subset(self) -> tuple[str, ...] | None:
        """Flat union of selected skills (``None`` = all) for any target."""
        return _sorted_names(_union_all(_names(p.skills) for p in self.pairings))

    @property
    def target_subset(self) -> tuple[str, ...] | None:
        """Flat union of selected targets (``None`` = every effective target)."""
        if any(p.targets is None for p in self.pairings):
            return None
        return tuple(sorted({t for p in self.pairings for t in p.targets or ()}))

    @property
    def is_product(self) -> bool:
        """True when ``skill_subset x target_subset`` encodes the selection exactly."""
        return len(self.pairings) == 1

    def selects_target(self, target: str) -> bool:
        """Return whether any edge selected *target*."""
        return any(p.targets is None or target in p.targets for p in self.pairings)

    def skills_for_target(self, target: str) -> tuple[str, ...] | None:
        """Return the skills selected for *target* (``None`` = all skills).

        Raises ``KeyError`` when no edge selected *target*; callers check
        :meth:`selects_target` first so "nothing" is never read as "all".
        """
        covering = [
            _names(p.skills) for p in self.pairings if p.targets is None or target in p.targets
        ]
        if not covering:
            raise KeyError(target)
        return _sorted_names(_union_all(covering))

    def merge(self, other: SkillTargetSelection) -> SkillTargetSelection:
        """Return the union of two node selections."""
        raw = _raw_from_pairings(self.pairings)
        _absorb_raw(raw, _raw_from_pairings(other.pairings))
        return SkillTargetSelection(_canonicalize(raw))

    def with_edge(
        self, skills: Iterable[str] | None, targets: Iterable[str] | None
    ) -> SkillTargetSelection:
        """Return this selection plus one more ``skills x targets`` edge."""
        return self.merge(merge_edge_pairings([(skills, targets)]))

    def to_lock_records(self) -> list[dict[str, list[str]]] | None:
        """Return ``subset_pairings`` records, or ``None`` when the flat fields suffice.

        Omitting the field for products keeps lockfiles byte-identical to
        earlier writers whenever pairing cannot change the deployment.
        """
        if self.is_product:
            return None
        return [pairing.to_lock_record() for pairing in self.pairings]

    @classmethod
    def from_lock_fields(
        cls,
        skill_subset: Iterable[str] | None,
        target_subset: Iterable[str] | None,
        records: object = None,
    ) -> SkillTargetSelection:
        """Decode a lock entry's selection.

        Without ``subset_pairings`` (any lockfile written before exact
        pairing), the selection is the product of the flat fields, which is
        exactly what those writers deployed. With records, the flat fields
        MUST equal the records' projections; a mismatch (stale or tampered
        state) raises ``ValueError``.
        """
        if records is None:
            return merge_edge_pairings([(skill_subset, target_subset)])
        selection = merge_edge_pairings(_parse_lock_records(records))
        recorded_skills = _sorted_names(_names(_normalize_axis(skill_subset, "skill_subset")))
        recorded_targets = _sorted_names(_names(_normalize_axis(target_subset, "target_subset")))
        if (selection.skill_subset, selection.target_subset) != (
            recorded_skills,
            recorded_targets,
        ):
            raise ValueError(
                f"{SUBSET_PAIRINGS_LOCK_FIELD} disagree with skill_subset/target_subset; "
                f"{SUBSET_PAIRINGS_INCONSISTENT.clause()}"
            )
        return selection


def dependency_pairing(dep_ref: Any) -> SkillTargetSelection:
    """Return the exact selection a resolved or replayed dependency carries."""
    pairing = getattr(dep_ref, "subset_pairings", None)
    if pairing is not None:
        return pairing
    return merge_edge_pairings(
        [(getattr(dep_ref, "skill_subset", None), getattr(dep_ref, "target_subset", None))]
    )


def merge_edge_pairings(
    edges: Iterable[tuple[Iterable[str] | None, Iterable[str] | None]],
) -> SkillTargetSelection:
    """Merge per-edge ``(skill_subset, target_subset)`` selections exactly.

    ``None`` or an empty collection on an axis means "all" for that edge,
    matching ``DependencyReference`` and lockfile conventions. The result
    deploys a skill to a target only when one edge selects both.
    """
    raw: _RawPairing = {}
    seen = False
    for skills, targets in edges:
        seen = True
        skill_set = _names(_normalize_axis(skills, "skills"))
        target_set = _names(_normalize_axis(targets, "targets"))
        edge_raw: _RawPairing = {}
        for target in target_set if target_set is not None else (_EVERY_TARGET,):
            edge_raw[target] = skill_set
        _absorb_raw(raw, edge_raw)
    if not seen:
        raise ValueError("A dependency selection needs at least one edge")
    return SkillTargetSelection(_canonicalize(raw))


_TARGET_AXES = frozenset({"targets", "target_subset"})


def _normalize_axis(values: Iterable[object] | None, axis: str) -> tuple[str, ...] | None:
    if values is None:
        return None
    if isinstance(values, str):
        raise ValueError(f"'{axis}' must be a list of names, not a string")
    names: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Each entry in '{axis}' must be a non-empty string")
        names.append(value.strip())
    if axis in _TARGET_AXES:
        # Recorded and declared targets compare by canonical identifier.
        return tuple(canonical_target_subset(names)) or None
    return tuple(names) or None


def _names(values: Iterable[str] | None) -> _SkillSet:
    return None if values is None else frozenset(values)


def _sorted_names(values: _SkillSet) -> tuple[str, ...] | None:
    return None if values is None else tuple(sorted(values))


def _union(left: _SkillSet, right: _SkillSet) -> _SkillSet:
    if left is None or right is None:
        return None
    return left | right


def _union_all(values: Iterable[_SkillSet]) -> _SkillSet:
    result: _SkillSet = frozenset()
    for value in values:
        result = _union(result, value)
        if result is None:
            return None
    return result


def _absorb_raw(into: _RawPairing, other: _RawPairing) -> None:
    for target, skills in other.items():
        into[target] = _union(into[target], skills) if target in into else skills


def _raw_from_pairings(pairings: Iterable[SubsetPairing]) -> _RawPairing:
    raw: _RawPairing = {}
    for pairing in pairings:
        targets = pairing.targets if pairing.targets is not None else (_EVERY_TARGET,)
        _absorb_raw(raw, dict.fromkeys(targets, _names(pairing.skills)))
    return raw


def _canonicalize(raw: _RawPairing) -> tuple[SubsetPairing, ...]:
    """Group targets by their effective skill set; drop targets equal to the wildcard."""
    has_everywhere = _EVERY_TARGET in raw
    everywhere = raw.get(_EVERY_TARGET)
    grouped: dict[_SkillSet, list[str]] = {}
    for target, skills in raw.items():
        if target is _EVERY_TARGET:
            continue
        effective = _union(skills, everywhere) if has_everywhere else skills
        if has_everywhere and effective == everywhere:
            continue
        grouped.setdefault(effective, []).append(target)
    concrete = sorted(
        (
            SubsetPairing(_sorted_names(skills), tuple(sorted(targets)))
            for skills, targets in grouped.items()
        ),
        key=lambda pairing: pairing.targets or (),
    )
    if not has_everywhere:
        return tuple(concrete)
    return (SubsetPairing(_sorted_names(everywhere), None), *concrete)


def _parse_lock_records(
    records: object,
) -> list[tuple[tuple[str, ...] | None, tuple[str, ...] | None]]:
    if not isinstance(records, list) or not records:
        raise ValueError(f"'{SUBSET_PAIRINGS_LOCK_FIELD}' must be a non-empty list")
    edges: list[tuple[tuple[str, ...] | None, tuple[str, ...] | None]] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError(f"Each '{SUBSET_PAIRINGS_LOCK_FIELD}' entry must be a mapping")
        unknown = set(record) - _PAIRING_RECORD_KEYS
        if unknown:
            raise ValueError(
                f"Unknown '{SUBSET_PAIRINGS_LOCK_FIELD}' keys: {', '.join(sorted(map(str, unknown)))}"
            )
        axes = []
        for axis in ("skills", "targets"):
            if axis not in record:
                axes.append(None)
                continue
            value = record[axis]
            if not isinstance(value, list) or not value:
                raise ValueError(
                    f"'{SUBSET_PAIRINGS_LOCK_FIELD}' {axis} must be a non-empty list; "
                    "omit it to select all"
                )
            axes.append(_normalize_axis(value, axis))
        edges.append((axes[0], axes[1]))
    return edges


def copy_subset_pairings(value: object) -> Any:
    """Copy raw ``subset_pairings`` records without validating them.

    Malformed values survive the round trip so audit and frozen install can
    reject them through ``LockedDependency.selection()`` (req-rs-018).
    """
    if value is None or value == []:
        return None
    if isinstance(value, list):
        return [
            {key: list(item) if isinstance(item, list) else item for key, item in record.items()}
            if isinstance(record, dict)
            else record
            for record in value
        ]
    return value
