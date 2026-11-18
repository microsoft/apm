"""Package-level manifest target declaration (``target:`` / ``targets:``).

Canonical owner of how an apm.yml's package targets are read. Dependency
``targets:`` subsets, ``compilation.target``, and policy targets are separate
decisions owned elsewhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

from ..core.apm_yml import parse_targets_field
from ..core.target_catalog import normalize_target_name
from ..core.target_detection import parse_target_field

_MANIFEST_TARGET_KEYS: tuple[str, ...] = ("target", "targets")
_LEGACY_ALL_TARGET = "all"


def _authored_target_tokens(value: Any) -> list[str]:
    if isinstance(value, str):
        return [token.strip().lower() for token in value.split(",") if token.strip()]
    if isinstance(value, list):
        return [str(token).strip().lower() for token in value if str(token).strip()]
    return []


@dataclass(frozen=True)
class ManifestTargetDeclaration:
    """The package-level ``target:`` / ``targets:`` declaration of one apm.yml.

    Canonical owner of how a manifest's target fields are read: which spelling
    was used, per-spelling token validation, legacy ``all``, and the views each
    consumer needs. Readers go through this type (``APMPackage.target_declaration``,
    :func:`package_target_declaration`, :func:`read_manifest_target_declaration`)
    instead of re-reading the raw keys. Dependency ``targets:`` subsets, the
    ``compilation.target`` field, and policy targets are different decisions.
    """

    fields: tuple[tuple[str, Any], ...] = ()
    source_path: Path | None = None

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any] | None,
        source_path: Path | None = None,
    ) -> ManifestTargetDeclaration:
        """Capture the authored target fields of a manifest mapping."""
        if not isinstance(data, Mapping):
            return cls(source_path=source_path)
        return cls(
            tuple((key, data[key]) for key in _MANIFEST_TARGET_KEYS if key in data),
            source_path,
        )

    @property
    def spelling(self) -> str | None:
        """``"target"``, ``"targets"``, or ``None`` when nothing is declared."""
        keys = [key for key, _value in self.fields]
        return keys[-1] if keys else None

    @cached_property
    def _validated(self) -> tuple[str | list[str] | None, tuple[str, ...]]:
        data = dict(self.fields)
        if "targets" in data:
            # Also rejects a manifest that declares both keys.
            parsed = parse_targets_field(data)
            return (list(parsed) or None), tuple(parsed)
        value = parse_target_field(data.get("target"), source_path=self.source_path)
        values = value if isinstance(value, list) else [value] if isinstance(value, str) else []
        return value, tuple(
            name if name == _LEGACY_ALL_TARGET else normalize_target_name(name) for name in values
        )

    @property
    def selection(self) -> str | list[str] | None:
        """Validated selection handed to target decisions (spelling-preserving).

        Singular ``target: all`` stays ``"all"`` (expanded by the decision);
        plural ``targets: [all]`` folds to ``None`` (unrestricted).
        """
        return self._validated[0]

    @property
    def canonical(self) -> tuple[str, ...]:
        """Validated target names with aliases resolved; singular ``all`` kept."""
        return self._validated[1]

    @property
    def legacy_all(self) -> bool:
        """Whether the manifest declares the legacy ``all`` token."""
        return any(
            _LEGACY_ALL_TARGET in _authored_target_tokens(value) for _key, value in self.fields
        )

    @property
    def explicit(self) -> bool:
        """A valid, nonempty declaration that does not rely on legacy ``all``."""
        return bool(self.selection) and not self.legacy_all

    def restriction(self, *, strict: bool = False) -> list[str] | None:
        """Declared names that restrict deployment, with legacy ``all`` folded.

        Validates with the plural vocabulary, so MCP-only and experimental
        tokens raise. Singular ``all`` folds without the plural deprecation
        warning because install expands it rather than treating it as omitted.
        Authoritative readers pass ``strict=True``: an empty singular
        ``target:`` and a singular ``all`` combined with other targets raise,
        as they do for install, instead of meaning "nothing declared".
        """
        data = dict(self.fields)
        if self.spelling == "target" and "targets" not in data and self.legacy_all:
            remaining = [
                token
                for token in _authored_target_tokens(data["target"])
                if token != _LEGACY_ALL_TARGET
            ]
            if remaining:
                parse_targets_field({"target": remaining})
                if strict:
                    self._raise_invalid_selection()
            return None
        names = parse_targets_field(data, allow_empty_singular=not strict)
        return names or None

    def _raise_invalid_selection(self) -> None:
        from ..core.errors import TargetResolutionError

        try:
            _ = self.selection
        except ValueError as exc:
            raise TargetResolutionError(str(exc)) from exc

    def authored_names(self) -> list[str]:
        """Unvalidated names as written, ``targets:`` first (for prompts only)."""
        data = dict(self.fields)
        value = data.get("targets")
        if value is None:
            value = data.get("target")
        if isinstance(value, list):
            return [str(name).strip() for name in value if str(name).strip()]
        if value is None:
            return []
        return [name.strip() for name in str(value).split(",") if name.strip()]

    def prefill_names(self) -> list[str]:
        """Names a re-init writes as ``targets:`` without changing what deploys.

        Singular ``target: all`` expands at install, so it is written as that
        explicit set; plural ``targets: [all]`` already means omitted.
        """
        if self.spelling == "target" and self.legacy_all:
            from ..core.errors import TargetResolutionError
            from ..core.target_catalog import expand_all

            try:
                selection = self.selection
            except (ValueError, TargetResolutionError):
                return self.authored_names()
            if selection == _LEGACY_ALL_TARGET:
                # Spelling-preserving, so ``vscode`` keeps its MCP destination.
                return list(expand_all("install"))
        return self.authored_names()

    def validate_for_install(self) -> None:
        """Reject declared targets ``apm install`` cannot deploy.

        Install calls this before writing anything, so a singular token the
        manifest parser accepts but install cannot deploy (``intellij``,
        ``copilot-app``) fails without leaving partial output behind.
        """
        if self.selection == _LEGACY_ALL_TARGET or not self.canonical:
            return
        parse_targets_field({"targets": list(self.canonical)})


def read_manifest_target_declaration(
    manifest_path: Path,
    *,
    lenient: bool = False,
) -> ManifestTargetDeclaration | None:
    """Load the target declaration of the apm.yml at *manifest_path*.

    Returns ``None`` when the file is absent. A strict read is authoritative:
    a symlink, a non-file, or a document that is not a YAML mapping raises
    (``OSError`` / ``yaml.YAMLError``), so a broken manifest never reads as
    "nothing declared" and silently widens the caller's target set. With
    *lenient*, unreadable or malformed manifests return ``None`` instead.
    Tokens are validated when a view (``selection``, ``restriction()``) is read.
    """
    from ..utils.yaml_io import load_yaml

    if lenient:
        try:
            if not manifest_path.is_file():
                return None
            data = load_yaml(manifest_path)
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            return None
        if not isinstance(data, Mapping):
            return None
        return ManifestTargetDeclaration.from_mapping(data, manifest_path)
    if manifest_path.is_symlink():
        raise OSError(f"{manifest_path.name} must be a regular, non-symlink file")
    if not manifest_path.exists():
        return None
    if not manifest_path.is_file():
        raise OSError(f"{manifest_path.name} must be a regular, non-symlink file")
    data = load_yaml(manifest_path)
    if not isinstance(data, Mapping):
        got = "an empty document" if data is None else str(type(data))
        raise yaml.YAMLError(f"{manifest_path.name} must contain a YAML object, got {got}")
    return ManifestTargetDeclaration.from_mapping(data, manifest_path)


def manifest_target_restriction(data: Mapping[str, Any] | None) -> list[str]:
    """Return the restricting target names of a manifest mapping (empty if none)."""
    return ManifestTargetDeclaration.from_mapping(data).restriction() or []
