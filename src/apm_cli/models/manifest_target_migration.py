"""Rewrite the deprecated top-level ``target:`` key of apm.yml to ``targets:``.

Only ``apm manifest migrate`` uses this module: install and every other
command never rewrite the manifest. The rewrite is comment-preserving and
touches only the top-level key; dependency ``targets:`` subsets are left
alone.

Mapping (each selected target is spelled by its MCP runtime identity, so the
MCP destination does not move):

* ``target: "a,b"`` / ``target: [a, b]`` -> ``targets: [a, b]``
* ``vscode`` stays ``vscode`` (keeps ``.vscode/mcp.json``)
* ``agents`` -> ``copilot``; ``agy`` -> ``antigravity``
* ``target: all`` -> the explicit list ``apm install`` expands it to today
* tokens with no ``targets:`` equivalent (``intellij``, ``copilot-app``, ...)
  are refused.

Every plan reads both declarations through ``ManifestTargetDeclaration`` and
is verified against the effective target decision owner: the legacy and
migrated declarations must select the same target profiles, MCP runtimes and
LSP targets, or the migration is refused.
"""

from __future__ import annotations

import difflib
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import TargetResolutionError
from ..core.target_detection import AgentsTargetDeprecationWarning, EffectiveTargetDecision
from ..utils.yaml_io import dump_yaml_roundtrip, load_yaml_roundtrip_str, yaml_roundtrip_to_str
from .manifest_targets import ManifestTargetDeclaration

_LEGACY_KEY = "target"
_CURRENT_KEY = "targets"
# Longer lists are written as a block list instead of a wrapped flow list.
_FLOW_LIST_MAX_WIDTH = 60


class ManifestMigrationError(ValueError):
    """The manifest cannot be migrated without changing what APM deploys."""


@dataclass(frozen=True)
class TargetMigrationPlan:
    """The ``targets:`` value that replaces a legacy ``target:`` value.

    ``targets`` is ``None`` when the legacy key is empty and is removed.
    """

    targets: tuple[str, ...] | None
    expanded_all: bool = False


@dataclass(frozen=True)
class ManifestMigration:
    """Result of planning a manifest migration; nothing is written yet."""

    path: Path
    original_text: str
    migrated_text: str
    plan: TargetMigrationPlan | None
    ignored_compilation_target: bool = False
    _document: Any = None

    @property
    def changed(self) -> bool:
        return self.plan is not None and self.original_text != self.migrated_text

    def diff(self, label: str = "apm.yml") -> str:
        """Return a unified diff of the proposed rewrite."""
        return "".join(
            difflib.unified_diff(
                self.original_text.splitlines(keepends=True),
                self.migrated_text.splitlines(keepends=True),
                fromfile=f"{label} (current)",
                tofile=f"{label} (migrated)",
                n=3,
            )
        )

    def write(self) -> None:
        """Write the migrated manifest (comment-preserving, LF line endings)."""
        if self.changed:
            dump_yaml_roundtrip(self._document, self.path)


def _no_equivalent(token: str) -> ManifestMigrationError:
    return ManifestMigrationError(
        f"Target '{token}' has no 'targets:' equivalent. Remove '{token}' from "
        f"'target:' in apm.yml and pass '--target {token}' to 'apm install' instead."
    )


def _decision_profile(decision: EffectiveTargetDecision) -> tuple[frozenset[str], ...]:
    return (
        frozenset(decision.canonical_targets or ()),
        frozenset(decision.runtime_targets or ()),
        frozenset(decision.lsp_targets or ()),
    )


def plan_target_migration(value: object, *, source_path: Path | None = None) -> TargetMigrationPlan:
    """Map a legacy ``target:`` value to an equivalent ``targets:`` list.

    Raises:
        ValueError: The legacy value is invalid (same error as install).
        ManifestMigrationError: A token has no ``targets:`` equivalent.
    """
    legacy_declaration = ManifestTargetDeclaration.from_mapping({_LEGACY_KEY: value}, source_path)
    with warnings.catch_warnings():
        # Migrating away from the alias is the fix this warning asks for.
        warnings.simplefilter("ignore", AgentsTargetDeprecationWarning)
        selection = legacy_declaration.selection
    if selection is None:
        return TargetMigrationPlan(targets=None)

    # The decision owner expands ``all`` and keeps each runtime spelling.
    legacy = EffectiveTargetDecision(selection, "apm.yml")
    targets = tuple(legacy.runtime_targets or ())
    for token in targets:
        try:
            _ = ManifestTargetDeclaration.from_mapping({_CURRENT_KEY: [token]}).selection
        except TargetResolutionError:
            raise _no_equivalent(token) from None

    migrated = EffectiveTargetDecision(
        ManifestTargetDeclaration.from_mapping({_CURRENT_KEY: list(targets)}).selection,
        "apm.yml",
    )
    if _decision_profile(migrated) != _decision_profile(legacy):
        raise ManifestMigrationError(
            f"Cannot migrate 'target:' in {source_path.name if source_path else 'apm.yml'} "
            f"automatically: 'targets: [{', '.join(targets)}]' would change what APM "
            "deploys. Replace 'target:' with a 'targets:' list by hand."
        )
    return TargetMigrationPlan(targets=targets, expanded_all=legacy_declaration.legacy_all)


def _move_key_comments(document: Any, old_key: str, new_key: str | None, position: int) -> None:
    """Re-attach comments of *old_key* to *new_key* (or the preceding key)."""
    comments = document.ca.items.pop(old_key, None)
    if comments is None:
        return
    if new_key is not None:
        document.ca.items[new_key] = comments
        return
    keys = list(document.keys())
    if position > 0 and keys[position - 1] not in document.ca.items:
        document.ca.items[keys[position - 1]] = comments


def _replace_sequence_items(sequence: Any, items: tuple[str, ...]) -> Any:
    """Rewrite a block list in place so per-item and trailing comments survive."""
    trailing = sequence.ca.items.pop(len(sequence) - 1, None) if len(sequence) else None
    for index, item in enumerate(items):
        if index < len(sequence):
            sequence[index] = item
        else:
            sequence.append(item)
    while len(sequence) > len(items):
        sequence.ca.items.pop(len(sequence) - 1, None)
        del sequence[len(sequence) - 1]
    if trailing is not None:
        sequence.ca.items[len(items) - 1] = trailing
    return sequence


def _rewrite_document(document: Any, plan: TargetMigrationPlan) -> None:
    from ruamel.yaml.comments import CommentedSeq

    keys = list(document.keys())
    position = keys.index(_LEGACY_KEY)
    legacy_value = document[_LEGACY_KEY]
    if plan.targets is None:
        _move_key_comments(document, _LEGACY_KEY, None, position)
        del document[_LEGACY_KEY]
        return

    if isinstance(legacy_value, CommentedSeq) and not legacy_value.fa.flow_style():
        value = _replace_sequence_items(legacy_value, plan.targets)
    else:
        value = CommentedSeq(plan.targets)
        if len(", ".join(plan.targets)) <= _FLOW_LIST_MAX_WIDTH:
            value.fa.set_flow_style()
        else:
            value.fa.set_block_style()
    comments = document.ca.items.get(_LEGACY_KEY)
    del document[_LEGACY_KEY]
    document.insert(position, _CURRENT_KEY, value)
    if comments is not None:
        document.ca.items[_CURRENT_KEY] = comments


def plan_manifest_migration(path: Path) -> ManifestMigration:
    """Plan the rewrite of *path* without writing it.

    A manifest without a top-level ``target:`` key is already migrated:
    the result reports no change.

    Raises:
        OSError: The manifest cannot be read.
        yaml.YAMLError: The manifest is not valid YAML.
        ValueError: The legacy value is invalid, or both keys are present.
        ManifestMigrationError: The value cannot be migrated.
    """
    from ruamel.yaml.comments import CommentedMap

    text = path.read_text(encoding="utf-8")
    document = load_yaml_roundtrip_str(text)
    if not isinstance(document, CommentedMap):
        raise ManifestMigrationError(f"{path} must contain a YAML mapping")
    compilation = document.get("compilation")
    # architecture-authority-exempt: compilation-target-field
    ignored_compilation_target = isinstance(compilation, dict) and "target" in compilation
    if _LEGACY_KEY not in document:
        return ManifestMigration(
            path=path,
            original_text=text,
            migrated_text=text,
            plan=None,
            ignored_compilation_target=ignored_compilation_target,
        )
    if _CURRENT_KEY in document:
        try:
            _ = ManifestTargetDeclaration.from_mapping(document, path).selection
        except TargetResolutionError as exc:
            raise ValueError(exc.format_message()) from None

    plan = plan_target_migration(document[_LEGACY_KEY], source_path=path)
    _rewrite_document(document, plan)
    return ManifestMigration(
        path=path,
        original_text=text,
        migrated_text=yaml_roundtrip_to_str(document),
        plan=plan,
        ignored_compilation_target=ignored_compilation_target,
        _document=document,
    )
