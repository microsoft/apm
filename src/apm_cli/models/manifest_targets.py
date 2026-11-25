"""Package-level manifest target declaration (``target:`` / ``targets:``).

Canonical owner of how an apm.yml's package targets are read, including the
once-per-process deprecation warning for the singular ``target:`` key (and the
ignored ``compilation.target``) in the consumer manifest, and of the one
mapping every apm.yml rewrite uses to turn a declaration into an equivalent
``targets:`` list (:func:`rewrite_target_names`, checked by
:func:`verify_rewritten_manifest` before the rewrite is written). Dependency
``targets:`` subsets, ``compilation.target`` resolution, and policy targets are
separate decisions owned elsewhere.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
import yaml

from ..core.apm_yml import parse_targets_field
from ..core.deprecations import deprecation_message
from ..core.target_catalog import (
    DEPRECATED_TARGET_ALIASES,
    canonical_target_token,
    deprecated_target_alias_message,
    first_deprecated_target_alias,
    is_vendor_target_id,
    normalize_target_name,
)
from ..core.target_detection import (
    AgentsTargetDeprecationWarning,
    EffectiveTargetDecision,
    parse_target_field,
)

if TYPE_CHECKING:
    from ..core.scope import InstallScope

_MANIFEST_TARGET_KEYS: tuple[str, ...] = ("target", "targets")
_LEGACY_ALL_TARGET = "all"

_deprecated_manifest_keys_warned: set[tuple[str, str]] = set()
_vendor_targets_warned: set[tuple[str, str]] = set()


def reset_manifest_deprecation_warnings() -> None:
    """Re-arm the once-per-process deprecation warnings. For tests only."""
    _deprecated_manifest_keys_warned.clear()
    _vendor_targets_warned.clear()


def _split_vendor_targets(names: Iterable[str]) -> tuple[list[str], tuple[str, ...]]:
    """Split ``x-<vendor>-<name>`` targets (req-tg-004) from deployable ones."""
    names = list(names)
    return (
        [name for name in names if not is_vendor_target_id(name)],
        tuple(name for name in names if is_vendor_target_id(name)),
    )


def _warn_vendor_targets(vendor: tuple[str, ...], manifest_path: Path) -> None:
    """Name each vendor target this APM has no handler for, once per process (#4251).

    Only the consumer manifest selects deployment targets, so only it warns.
    """
    from ..core.scope import InstallScope
    from ..utils.console import _rich_warning

    scope = _consumer_manifest_scope(manifest_path)
    if scope is None:
        return
    label = (
        "apm.yml"
        if scope is InstallScope.PROJECT
        else ascii(Path(manifest_path).resolve().as_posix())[1:-1]
    )
    for name in vendor:
        if (scope.value, name) in _vendor_targets_warned:
            continue
        _vendor_targets_warned.add((scope.value, name))
        _rich_warning(
            f"{label} declares vendor target '{name}', which has no registered handler "
            "in this APM version; nothing is deployed for it.",
            symbol="warning",
            err=True,
        )


def _consumer_manifest_scope(manifest_path: Path) -> InstallScope | None:
    """Return the scope whose consumer manifest is *manifest_path*, else None.

    Only the project ``apm.yml`` and the user ``~/.apm/apm.yml`` are consumer
    manifests; dependency manifests under ``apm_modules/`` never match.
    """
    from ..core.scope import InstallScope, get_manifest_path

    try:
        resolved = manifest_path.resolve()
    except OSError:
        return None
    for scope in (InstallScope.USER, InstallScope.PROJECT):
        try:
            if get_manifest_path(scope).resolve() == resolved:
                return scope
        except (OSError, ValueError):
            continue
    return None


def _warn_deprecated_manifest_keys(data: Mapping[str, Any], manifest_path: Path) -> None:
    """Warn once per process and scope about deprecated keys in a consumer manifest.

    Covers the singular top-level ``target:`` key, the ignored
    ``compilation.target`` key, and a deprecated target alias (``vscode``)
    under ``target:``, ``targets:`` or a dependency's ``targets:``. Writes to
    stderr and never changes the exit code, so ``--frozen`` and ``--ci`` runs
    keep their outcome.
    """
    compilation = data.get("compilation")
    # An alias under singular ``target:`` is covered by that key's warning,
    # whose replacement already writes the target the alias selects.
    alias, alias_key = _deprecated_alias_outside_target(data)
    present = [
        key
        for key, declared in (
            ("target", "target" in data),
            ("compilation.target", isinstance(compilation, Mapping) and "target" in compilation),
            (alias, alias is not None),
        )
        if declared
    ]
    if not present:
        return
    scope = _consumer_manifest_scope(manifest_path)
    if scope is None:
        return
    pending = [key for key in present if (scope.value, key) not in _deprecated_manifest_keys_warned]

    from ..core.scope import InstallScope
    from ..utils.console import _rich_warning

    user_scope = scope is InstallScope.USER
    # Resolve: `apm update -g` reads the user manifest by a relative path (C5b).
    label = ascii(Path(manifest_path).resolve().as_posix())[1:-1] if user_scope else "apm.yml"
    for key in pending:
        _deprecated_manifest_keys_warned.add((scope.value, key))
        if key == "target":
            declaration = ManifestTargetDeclaration.from_mapping(
                {"target": data["target"]}, manifest_path
            )
            message = deprecation_message(
                "manifest-target-key",
                label=label,
                hint=_target_replacement_hint(declaration, user_scope=user_scope),
            )
        elif key == "compilation.target":
            message = deprecation_message("manifest-compilation-target", label=label)
        else:
            message = deprecated_target_alias_message(key, where=label, key=alias_key)
        _rich_warning(message, symbol="warning", err=True)


def _deprecated_alias_outside_target(data: Mapping[str, Any]) -> tuple[str | None, str]:
    """The first deprecated alias under ``targets:`` or a dependency's ``targets:``."""
    alias = first_deprecated_target_alias(_authored_target_tokens(data.get("targets")))
    if alias is not None:
        return alias, "'targets:'"
    for section in ("dependencies", "devDependencies"):
        block = data.get(section)
        entries = block.get("apm") if isinstance(block, Mapping) else None
        for entry in entries if isinstance(entries, list) else ():
            if isinstance(entry, Mapping):
                alias = first_deprecated_target_alias(_authored_target_tokens(entry.get("targets")))
                if alias is not None:
                    return alias, "a dependency's 'targets:'"
    return None, ""


def _yaml_flow(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(str(item).strip() for item in value) + "]"
    return ascii(str(value).strip())[1:-1]


def _target_replacement_hint(declaration: ManifestTargetDeclaration, *, user_scope: bool) -> str:
    """Tell the user exactly what to write instead of their ``target:`` value."""
    value = dict(declaration.fields)["target"]
    authored = f"target: {_yaml_flow(value)}"
    replacement = declaration.targets_replacement(user_scope=user_scope)
    if replacement is None:
        return (
            f"'targets:' has no exact equivalent of '{authored}' here; list the targets "
            "you use, e.g. 'targets: [claude, copilot]'."
        )
    if not replacement:
        return "Delete the empty 'target:' key."
    hint = f"Replace '{authored}' with 'targets: {_yaml_flow(list(replacement))}'."
    if declaration.legacy_all:
        hint += " ('targets: [all]' means auto-detect, not every target.)"
    alias = first_deprecated_target_alias(_authored_target_tokens(value))
    if alias is not None:
        replacement_target = DEPRECATED_TARGET_ALIASES[alias]
        hint += (
            f" (`{alias}` is a deprecated alias: replace `{alias}` with `{replacement_target}`.)"
        )
    return hint


def canonical_target_names(names: Iterable[str]) -> tuple[str, ...]:
    """Resolve declared target names to canonical ones, keeping legacy ``all``.

    The single alias rule for both ``target:`` and ``targets:``: ``agents``
    becomes ``copilot``.
    """
    return tuple(
        name if name == _LEGACY_ALL_TARGET else normalize_target_name(name) for name in names
    )


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

    @classmethod
    def from_manifest(
        cls,
        data: Mapping[str, Any],
        manifest_path: Path,
        *,
        warn_deprecated: bool = True,
    ) -> ManifestTargetDeclaration:
        """Capture the declaration of the apm.yml at *manifest_path*.

        When *manifest_path* is the consumer manifest (project ``apm.yml`` or
        ``~/.apm/apm.yml``), warns once per process on stderr about the
        deprecated ``target:`` key and the ignored ``compilation.target``.
        An invalid declaration does not warn: the caller reports its error.
        """
        declaration = cls.from_mapping(data, manifest_path)
        if warn_deprecated and declaration._is_valid():
            _warn_deprecated_manifest_keys(data, manifest_path)
            if declaration.vendor_targets:
                _warn_vendor_targets(declaration.vendor_targets, manifest_path)
        return declaration

    def _is_valid(self) -> bool:
        try:
            _ = self._validated
        except (ValueError, TypeError, click.UsageError):
            return False
        return True

    @property
    def spelling(self) -> str | None:
        """``"target"``, ``"targets"``, or ``None`` when nothing is declared."""
        keys = [key for key, _value in self.fields]
        return keys[-1] if keys else None

    @cached_property
    def _parsed(self) -> tuple[str | list[str] | None, list[str]]:
        data = dict(self.fields)
        if "targets" in data:
            # Also rejects a manifest that declares both keys.
            parsed = parse_targets_field(data)
            return (list(parsed) or None), list(parsed)
        value = parse_target_field(data.get("target"), source_path=self.source_path)
        values = value if isinstance(value, list) else [value] if isinstance(value, str) else []
        return value, values

    @cached_property
    def _validated(self) -> tuple[str | list[str] | None, tuple[str, ...]]:
        value, values = self._parsed
        names, vendor = _split_vendor_targets(values)
        if vendor:
            # Vendor targets parse but select nothing APM can deploy (req-tg-004).
            value = (names or None) if isinstance(value, list) else None
        return value, canonical_target_names(names)

    @property
    def vendor_targets(self) -> tuple[str, ...]:
        """Declared ``x-<vendor>-<name>`` targets, which this APM cannot deploy."""
        return _split_vendor_targets(self._parsed[1])[1]

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
        names, _vendor = _split_vendor_targets(
            parse_targets_field(data, allow_empty_singular=not strict)
        )
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

    def targets_replacement(self, *, user_scope: bool = False) -> tuple[str, ...] | None:
        """The ``targets:`` list the deprecated ``target:`` warning suggests.

        A thin view over :func:`rewrite_target_names` (the one target rewrite
        mapping, shared with ``apm init`` re-init), so the suggestion always
        deploys what the declaration deploys. Singular ``all`` becomes the
        explicit set install expands it to, because plural ``targets: [all]``
        means "omitted" (auto-detect) instead.

        Returns ``()`` for an empty ``target:`` (delete the key) and ``None``
        when no list deploys identically: an invalid value, a token without a
        ``targets:`` equivalent (``intellij``), a ``targets:`` declaration, or
        ``all`` in the user manifest (``apm compile -g`` treats it as every
        known target, wider than any list).
        """
        if self.spelling != "target":
            return None
        if user_scope and self.legacy_all:
            return None
        try:
            return rewrite_target_names(self) or ()
        except (ValueError, TypeError, click.UsageError):
            return None

    def validate_for_install(self) -> None:
        """Reject declared targets ``apm install`` cannot deploy.

        Install calls this before writing anything, so a singular token the
        manifest parser accepts but install cannot deploy (``intellij``,
        ``copilot-app``) fails without leaving partial output behind.
        """
        if self.selection == _LEGACY_ALL_TARGET or not self.canonical:
            return
        parse_targets_field({"targets": list(self.canonical)})


class TargetRewriteError(ValueError):
    """A declaration has no ``targets:`` list that deploys exactly the same.

    ``token`` names the target with no ``targets:`` spelling, when that is
    the reason.
    """

    def __init__(self, message: str, *, token: str | None = None) -> None:
        super().__init__(message)
        self.token = token


def _no_targets_equivalent(token: str) -> TargetRewriteError:
    return TargetRewriteError(
        f"Target '{token}' has no 'targets:' equivalent. Remove '{token}' from "
        f"'target:' in apm.yml and pass '--target {token}' to 'apm install' instead.",
        token=token,
    )


def _decision_profile(decision: Any) -> tuple[frozenset[str], ...]:
    return (
        frozenset(decision.canonical_targets or ()),
        frozenset(decision.runtime_targets or ()),
        frozenset(decision.lsp_targets or ()),
    )


def rewrite_target_names(declaration: ManifestTargetDeclaration) -> tuple[str, ...] | None:
    """Return the ``targets:`` list that deploys what *declaration* deploys.

    The one target rewrite mapping for apm.yml. A singular ``target:`` value is
    spelled by the runtime identities the effective target decision keeps
    (``agents`` -> ``copilot``, ``agy`` -> ``antigravity``, a CSV becomes a
    list, legacy ``all`` expands to what ``apm install`` deploys today); a
    plural ``targets:`` list is kept as written. ``None`` when nothing is
    selected.

    Raises:
        ValueError, click.UsageError: The declaration is invalid (as for install).
        TargetRewriteError: No ``targets:`` list deploys the same targets.
    """

    from ..core.errors import TargetResolutionError

    if declaration.spelling != "target":
        # Deprecated aliases (``vscode``) are written as the target they select.
        names = list(
            dict.fromkeys(
                canonical_target_token(name.lower()) for name in declaration.authored_names()
            )
        )
        if not names:
            return None
        # Also refuses a manifest that declares both keys.
        rewritten = {**dict(declaration.fields), "targets": names}
        _ = ManifestTargetDeclaration.from_mapping(rewritten, declaration.source_path).selection
        return tuple(names)
    with warnings.catch_warnings():
        # Rewriting away from the alias is the fix this warning asks for.
        warnings.simplefilter("ignore", AgentsTargetDeprecationWarning)
        selection = declaration.selection
    if selection is None:
        return None
    # The decision owner expands ``all`` and keeps each runtime spelling.
    legacy = EffectiveTargetDecision(selection, "apm.yml")
    targets = tuple(legacy.runtime_targets or ())
    for token in targets:
        try:
            _ = ManifestTargetDeclaration.from_mapping({"targets": [token]}).selection
        except TargetResolutionError:
            raise _no_targets_equivalent(token) from None
    rewritten_selection = ManifestTargetDeclaration.from_mapping({"targets": list(targets)})
    rewritten = EffectiveTargetDecision(rewritten_selection.selection, "apm.yml")
    if _decision_profile(rewritten) != _decision_profile(legacy):
        name = declaration.source_path.name if declaration.source_path else "apm.yml"
        raise TargetRewriteError(
            f"'target:' in {name} cannot be rewritten automatically: "
            f"'targets: [{', '.join(targets)}]' would change what APM deploys. "
            "Replace 'target:' with a 'targets:' list by hand."
        )
    return targets


# IDEs are clients of a target, not targets: none can be recorded under
# ``targets:`` (#3878). ``--target intellij`` still configures JetBrains
# Copilot's MCP file for one install; it is never written to apm.yml.
_IDE_TARGETS: frozenset[str] = frozenset({"intellij"})


class IdeTargetError(TargetRewriteError):
    """An IDE (``intellij``) was selected where apm.yml would record targets."""


def _refuse_ide_targets(names: Iterable[str]) -> None:
    for name in names:
        for token in str(name).split(","):
            token = token.strip().lower()
            if token in _IDE_TARGETS:
                raise IdeTargetError(
                    f"'{token}' is an IDE, not a target, so apm.yml cannot record it. "
                    "Use '--target copilot' for GitHub Copilot in JetBrains IDEs; "
                    "once apm.yml exists, '--target intellij' writes JetBrains MCP config.",
                    token=token,
                )


def option_target_names(target: str | list[str] | None) -> list[str] | None:
    """Return the ``targets:`` a new apm.yml records for a ``--target`` value.

    The one place that turns an install/init ``--target`` value into manifest
    tokens (#3878): ``all`` expands to what install deploys, runtime spellings
    fold to their primitive profile, and tokens with no manifest spelling
    are left out. ``None`` keeps auto-detection.

    Raises:
        IdeTargetError: An IDE (``intellij``) was selected; apm.yml never
            records one (``.token`` names it).
    """
    if target is None:
        return None
    _refuse_ide_targets([target] if isinstance(target, str) else target)

    from ..core.apm_yml import CANONICAL_TARGETS
    from ..core.target_catalog import canonical_target_token, expand_all, get_target_capability

    raw_targets = [target] if isinstance(target, str) else list(target)
    seen: set[str] = set()
    manifest_targets: list[str] = []
    for raw_target in raw_targets:
        expanded = (
            expand_all("install")
            if raw_target == "all"
            else [canonical_target_token(str(raw_target))]
        )
        for item in expanded:
            capability = get_target_capability(item)
            canonical = (
                capability.primitive_profile if item in capability.runtimes else capability.name
            )
            if canonical in CANONICAL_TARGETS and canonical not in seen:
                seen.add(canonical)
                manifest_targets.append(canonical)
    return sorted(manifest_targets) if manifest_targets else None


def bootstrap_target_names(names: Iterable[str]) -> tuple[str, ...]:
    """Return the ``targets:`` a new manifest records for explicitly selected targets.

    The selection is spelled through :func:`rewrite_target_names`, so a new
    manifest never records a token the manifest reader rejects.

    Raises:
        IdeTargetError: An IDE (``intellij``) was selected.
        TargetRewriteError: A selected target has no ``targets:`` spelling
            (``.token`` names it).
    """
    selected = list(names)
    if not selected:
        return ()
    _refuse_ide_targets(selected)
    return rewrite_target_names(ManifestTargetDeclaration.from_mapping({"target": selected})) or ()


def verify_rewritten_manifest(path: Path, text: str) -> None:
    """Refuse a rewritten apm.yml that APM would reject when it reads it back.

    Parses *text* with the apm.yml read checks and validates its target
    declaration the way install does. Call before writing *text* to *path*.

    Raises:
        TargetRewriteError: The rewrite would leave an invalid manifest.
    """
    from .manifest_io import load_manifest_text

    try:
        data = load_manifest_text(path, text)
        if not isinstance(data, Mapping):
            raise ValueError(f"{path} must contain a YAML mapping")
        ManifestTargetDeclaration.from_mapping(data, path).validate_for_install()
    except (ValueError, TypeError, yaml.YAMLError, click.UsageError) as exc:
        detail = exc.format_message() if isinstance(exc, click.ClickException) else str(exc)
        raise TargetRewriteError(
            f"Refusing to write {path.name}: APM would reject the rewritten manifest. "
            f"{detail.strip()}"
        ) from exc


def read_manifest_target_declaration(
    manifest_path: Path,
    *,
    lenient: bool = False,
) -> ManifestTargetDeclaration | None:
    """Load the target declaration of the apm.yml at *manifest_path*.

    Returns ``None`` when the file is absent. A strict read is authoritative:
    a symlink, a non-file, or a document that is not a YAML mapping raises
    (``OSError`` / ``yaml.YAMLError``), and a manifest the read owner refuses
    (duplicate key, YAML anchor or alias) raises ``InvalidManifestError``, so
    a broken manifest never reads as
    "nothing declared" and silently widens the caller's target set. With
    *lenient*, unreadable or malformed manifests return ``None`` instead.
    Tokens are validated when a view (``selection``, ``restriction()``) is read.
    """
    from .manifest_io import load_manifest

    if lenient:
        try:
            if not manifest_path.is_file():
                return None
            data = load_manifest(manifest_path)
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            return None
        if not isinstance(data, Mapping):
            return None
        return ManifestTargetDeclaration.from_manifest(data, manifest_path)
    if manifest_path.is_symlink():
        raise OSError(f"{manifest_path.name} must be a regular, non-symlink file")
    if not manifest_path.exists():
        return None
    if not manifest_path.is_file():
        raise OSError(f"{manifest_path.name} must be a regular, non-symlink file")
    try:
        data = load_manifest(manifest_path)
    except UnicodeDecodeError as exc:
        # Same contract as the bounded YAML loader: undecodable bytes are YAML errors.
        raise yaml.YAMLError(f"UnicodeDecodeError: {exc}") from exc
    if not isinstance(data, Mapping):
        got = "an empty document" if data is None else str(type(data))
        raise yaml.YAMLError(f"{manifest_path.name} must contain a YAML object, got {got}")
    return ManifestTargetDeclaration.from_manifest(data, manifest_path)


def manifest_target_restriction(
    data: Mapping[str, Any] | None, manifest_path: Path | None = None
) -> list[str]:
    """Return the restricting target names of a manifest mapping (empty if none).

    With *manifest_path*, the read also emits the deprecated-key warnings of
    :meth:`ManifestTargetDeclaration.from_manifest` (once per run), so raw-YAML
    readers such as ``apm pack`` warn like every ``APMPackage`` reader.
    """
    if manifest_path is not None and isinstance(data, Mapping):
        declaration = ManifestTargetDeclaration.from_manifest(data, manifest_path)
    else:
        declaration = ManifestTargetDeclaration.from_mapping(data)
    return declaration.restriction() or []
