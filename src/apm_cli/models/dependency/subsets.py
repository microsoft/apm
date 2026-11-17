"""Per-dependency subset field parsers and the selection-merge owner.

One dependency node can be declared by several edges (the project's
``apm.yml`` and any number of parent packages). Each edge may narrow the node
with ``skills:`` and ``targets:``. :func:`merge_edge_selections` is the single
owner of the selection the node carries: a per-field union in which an
unrestricted edge (``None``) absorbs every narrowing, returned as a sorted
tuple so the result never depends on traversal order (#3735).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

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
            target_subset=_normalized(getattr(dep_ref, "target_subset", None)),
            alias=getattr(dep_ref, "alias", None),
            allow_insecure=bool(getattr(dep_ref, "allow_insecure", False)),
        )

    def declared_by(self) -> str:
        """Return a printable name for the manifest that declared this edge."""
        return " > ".join(self.parent_chain) if self.parent_chain else ROOT_MANIFEST_LABEL


@dataclass(frozen=True)
class NodeSelection:
    """The merged selection one dependency node carries.

    ``contributors`` keeps every edge record, in discovery order, for
    diagnostics; it is never persisted.
    """

    skill_subset: tuple[str, ...] | None
    target_subset: tuple[str, ...] | None
    contributors: tuple[EdgeSelection, ...]

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

    Per field: union of the declared names, where any unrestricted edge
    (``None``) absorbs the field to ``None``. The result is a sorted tuple, so
    the merge is commutative, associative and idempotent.
    """
    contributors = tuple(edges)
    if not contributors:
        raise SelectionMergeError("A dependency node needs at least one declaring edge")
    unsupported = sorted({edge.mode for edge in contributors} - {"include"})
    if unsupported:
        raise SelectionMergeError(
            f"Cannot merge {', '.join(unsupported)} selections with other declarations"
        )
    return NodeSelection(
        skill_subset=_merge_field(edge.skill_subset for edge in contributors),
        target_subset=_merge_field(edge.target_subset for edge in contributors),
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
