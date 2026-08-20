"""Per-dependency subset field parsers."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath

from apm_cli.utils.path_security import validate_path_segments


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
