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
from ..utils.atomic_io import atomic_write_text
from ..utils.yaml_io import load_yaml_roundtrip_str, yaml_roundtrip_to_str
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
        """Atomically write the migrated manifest (comment-preserving, LF line endings).

        A failed write (disk full, quota) leaves the original manifest intact.
        """
        if self.changed:
            atomic_write_text(self.path, self.migrated_text)


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


def plan_target_migration(
    value: object, *, source_path: Path | None = None, user_scope: bool = False
) -> TargetMigrationPlan:
    """Map a legacy ``target:`` value to an equivalent ``targets:`` list.

    With *user_scope*, the user manifest also drives ``apm compile -g``, which
    writes every known target when the declaration does not restrict them.

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

    migrated_declaration = ManifestTargetDeclaration.from_mapping({_CURRENT_KEY: list(targets)})
    migrated = EffectiveTargetDecision(migrated_declaration.selection, "apm.yml")
    if user_scope and legacy_declaration.legacy_all:
        # Singular ``all`` leaves ``apm compile -g`` unrestricted; any list narrows it.
        label = source_path.as_posix() if source_path else "~/.apm/apm.yml"
        raise ManifestMigrationError(
            f"Cannot migrate 'target: all' in {label} automatically: 'apm compile -g' "
            "writes every known target for it, including targets 'apm install -g' does "
            "not deploy, and any 'targets:' list would narrow that. Replace 'target: all' "
            "by hand with the 'targets:' you use."
        )
    if _decision_profile(migrated) != _decision_profile(legacy):
        raise ManifestMigrationError(
            f"Cannot migrate 'target:' in {source_path.name if source_path else 'apm.yml'} "
            f"automatically: 'targets: [{', '.join(targets)}]' would change what APM "
            "deploys. Replace 'target:' with a 'targets:' list by hand."
        )
    return TargetMigrationPlan(targets=targets, expanded_all=legacy_declaration.legacy_all)


def _comment_tokens(entry: Any) -> list[Any]:
    tokens: list[Any] = []
    for slot in entry or ():
        if isinstance(slot, list):
            tokens.extend(token for token in slot if token is not None)
        elif slot is not None:
            tokens.append(slot)
    return tokens


def _keep_removed_key_comments(document: Any, key: str, position: int) -> None:
    """Keep the comments of a removed key on its neighbour so none are lost."""
    from ruamel.yaml.tokens import CommentToken

    tokens = _comment_tokens(document.ca.items.pop(key, None))
    if not tokens:
        return
    text = "".join(token.value for token in tokens)
    if not text.endswith("\n"):
        text += "\n"
    keys = [name for name in document if name != key]
    if position > 0:
        previous = keys[position - 1]
        entry = document.ca.items.setdefault(previous, [None, None, None, None])
        existing = entry[2]
        if existing is None:
            entry[2] = CommentToken(text, tokens[0].start_mark)
        else:
            existing.value = existing.value.rstrip("\n") + "\n" + text
        return
    lines = "\n".join(line.lstrip("#").strip() for line in text.splitlines() if line.strip())
    if keys:
        document.yaml_set_comment_before_after_key(keys[0], before=lines)
    else:
        document.yaml_set_start_comment(lines)


def _replace_sequence_items(sequence: Any, items: tuple[str, ...]) -> Any:
    """Rewrite a list in place so per-item and trailing comments survive."""
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


def _detached_sequence(sequence: Any) -> Any:
    """Copy *sequence* with its comments and style, but not its anchor.

    An anchored ``target:`` list may be aliased elsewhere in the manifest;
    rewriting a copy keeps those aliases unchanged.
    """
    from ruamel.yaml.comments import CommentedSeq

    copy = CommentedSeq(list(sequence))
    copy.ca.items.update(sequence.ca.items)
    copy.ca.comment = sequence.ca.comment
    if sequence.fa.flow_style():
        copy.fa.set_flow_style()
    return copy


def _flow_comments_to_block(sequence: Any) -> None:
    """Switch a commented flow list to block style so its comments survive.

    ruamel drops comments inside a flow list when it writes the list back, so
    each comment moves to the end of the line of the item before it.
    """
    from ruamel.yaml.error import CommentMark
    from ruamel.yaml.tokens import CommentToken

    moved: dict[int, list[Any]] = {}
    for index, entry in sequence.ca.items.items():
        tokens = _comment_tokens(entry)
        if tokens:
            moved.setdefault(max(index - 1, 0), []).extend(tokens)
    sequence.ca.items.clear()
    for index, tokens in moved.items():
        text = "".join(token.value for token in tokens)
        sequence.ca.items[index] = [CommentToken(text, CommentMark(0)), None]
    sequence.fa.set_block_style()


def _rewrite_document(document: Any, plan: TargetMigrationPlan) -> None:
    from ruamel.yaml.comments import CommentedSeq

    keys = list(document.keys())
    position = keys.index(_LEGACY_KEY)
    legacy_value = document[_LEGACY_KEY]
    if plan.targets is None:
        _keep_removed_key_comments(document, _LEGACY_KEY, position)
        del document[_LEGACY_KEY]
        return

    fits_flow = len(", ".join(plan.targets)) <= _FLOW_LIST_MAX_WIDTH
    if isinstance(legacy_value, CommentedSeq):
        # Rewrite the authored list so per-item comments survive.
        sequence = _detached_sequence(legacy_value) if legacy_value.anchor.value else legacy_value
        if sequence.fa.flow_style() and sequence.ca.items:
            _flow_comments_to_block(sequence)
        value = _replace_sequence_items(sequence, plan.targets)
        if value.fa.flow_style() and not fits_flow:
            value.fa.set_block_style()
    else:
        value = CommentedSeq(plan.targets)
        if fits_flow:
            value.fa.set_flow_style()
        else:
            value.fa.set_block_style()
    comments = document.ca.items.get(_LEGACY_KEY)
    del document[_LEGACY_KEY]
    document.insert(position, _CURRENT_KEY, value)
    if comments is not None:
        document.ca.items[_CURRENT_KEY] = comments


def plan_manifest_migration(path: Path, *, user_scope: bool = False) -> ManifestMigration:
    """Plan the rewrite of *path* without writing it.

    *user_scope* marks the user manifest (``~/.apm/apm.yml``), which also
    drives ``apm compile -g``.

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

    plan = plan_target_migration(document[_LEGACY_KEY], source_path=path, user_scope=user_scope)
    _rewrite_document(document, plan)
    return ManifestMigration(
        path=path,
        original_text=text,
        migrated_text=yaml_roundtrip_to_str(document),
        plan=plan,
        ignored_compilation_target=ignored_compilation_target,
    )
