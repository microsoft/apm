"""Rewrite the deprecated top-level ``target:`` key of apm.yml to ``targets:``.

Only ``apm manifest migrate`` uses this module: it rewrites the key in place
(comment-preserving, top-level key only; dependency ``targets:`` subsets are
left alone). The token mapping belongs to the declaration owner
(``manifest_targets.rewrite_target_names``), which the ``apm init`` re-init
uses too; the rewritten text is checked with
``manifest_targets.verify_rewritten_manifest`` before it is written.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import TargetResolutionError
from ..utils.atomic_io import atomic_write_text
from ..utils.yaml_io import load_yaml_roundtrip_str, yaml_roundtrip_to_str
from .manifest_io import read_manifest_text
from .manifest_targets import (
    ManifestTargetDeclaration,
    TargetRewriteError,
    rewrite_target_names,
    verify_rewritten_manifest,
)

_LEGACY_KEY = "target"
_CURRENT_KEY = "targets"
# Longer lists are written as a block list instead of a wrapped flow list.
_FLOW_LIST_MAX_WIDTH = 60


# The manifest cannot be migrated without changing what APM deploys.
ManifestMigrationError = TargetRewriteError


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


def plan_target_migration(
    value: object, *, source_path: Path | None = None, user_scope: bool = False
) -> TargetMigrationPlan:
    """Map a legacy ``target:`` value to an equivalent ``targets:`` list.

    Delegates to the declaration owner's rewrite mapping
    (:func:`~apm_cli.models.manifest_targets.rewrite_target_names`). With
    *user_scope*, the user manifest also drives ``apm compile -g``, which
    writes every known target when the declaration does not restrict them.

    Raises:
        ValueError: The legacy value is invalid (same error as install).
        ManifestMigrationError: A token has no ``targets:`` equivalent.
    """
    legacy_declaration = ManifestTargetDeclaration.from_mapping({_LEGACY_KEY: value}, source_path)
    targets = rewrite_target_names(legacy_declaration)
    if targets is None:
        return TargetMigrationPlan(targets=None)
    if user_scope and legacy_declaration.legacy_all:
        # Singular ``all`` leaves ``apm compile -g`` unrestricted; any list narrows it.
        label = source_path.as_posix() if source_path else "~/.apm/apm.yml"
        raise ManifestMigrationError(
            f"Cannot migrate 'target: all' in {label} automatically: 'apm compile -g' "
            "writes every known target for it, including targets 'apm install -g' does "
            "not deploy, and any 'targets:' list would narrow that. Replace 'target: all' "
            "by hand with the 'targets:' you use."
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
        sequence = legacy_value
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
        ValueError: The legacy value is invalid, both keys are present, or
            the manifest uses a YAML anchor, alias or duplicate top-level key.
        ManifestMigrationError: The value cannot be migrated.
    """
    from ruamel.yaml.comments import CommentedMap

    text = read_manifest_text(path)
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
    migrated_text = yaml_roundtrip_to_str(document)
    verify_rewritten_manifest(path, migrated_text)
    return ManifestMigration(
        path=path,
        original_text=text,
        migrated_text=migrated_text,
        plan=plan,
        ignored_compilation_target=ignored_compilation_target,
    )
