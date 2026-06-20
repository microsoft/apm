"""Validation logic and type enums for APM packages."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..constants import APM_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME
from ._validation_rules import (
    _validate_apm_package_with_yml as _validate_apm_package_with_yml,
)
from ._validation_rules import (
    _validate_claude_skill as _validate_claude_skill,
)
from ._validation_rules import (
    _validate_hook_package as _validate_hook_package,
)
from ._validation_rules import (
    _validate_hybrid_package as _validate_hybrid_package,
)
from ._validation_rules import (
    _validate_marketplace_plugin as _validate_marketplace_plugin,
)
from ._validation_rules import (
    _validate_skill_bundle as _validate_skill_bundle,
)

if TYPE_CHECKING:
    from .apm_package import APMPackage


class PackageType(Enum):
    """Types of packages that APM can install.

    This enum is used internally to classify packages based on their content
    (presence of apm.yml, SKILL.md, hooks/, plugin.json, etc.).
    """

    APM_PACKAGE = "apm_package"  # Has apm.yml (.apm/ optional when deps declared)
    CLAUDE_SKILL = "claude_skill"  # Has SKILL.md, no apm.yml
    HOOK_PACKAGE = "hook_package"  # Has hooks/hooks.json, no apm.yml or SKILL.md
    HYBRID = "hybrid"  # Has both apm.yml and SKILL.md (root)
    MARKETPLACE_PLUGIN = "marketplace_plugin"  # Has plugin.json or .claude-plugin/
    SKILL_BUNDLE = "skill_bundle"  # Has skills/<name>/SKILL.md (nested), apm.yml optional
    INVALID = "invalid"  # None of the above


class PackageContentType(Enum):
    """Explicit package content type declared in apm.yml.

    This is the user-facing `type` field in apm.yml that controls how the
    package is processed during install/compile:
    - INSTRUCTIONS: Compile to AGENTS.md only, no skill created
    - SKILL: Install as native skill only, no AGENTS.md compilation
    - HYBRID: Both AGENTS.md instructions AND skill installation (default)
    - PROMPTS: Commands/prompts only, no instructions or skills
    """

    INSTRUCTIONS = "instructions"  # Compile to AGENTS.md only
    SKILL = "skill"  # Install as native skill only
    HYBRID = "hybrid"  # Both (default)
    PROMPTS = "prompts"  # Commands/prompts only

    @classmethod
    def from_string(cls, value: str) -> PackageContentType:
        """Parse a string value into a PackageContentType enum.

        Args:
            value: String value to parse (e.g., "instructions", "skill")

        Returns:
            PackageContentType: The corresponding enum value

        Raises:
            ValueError: If the value is not a valid package content type
        """
        if not value:
            raise ValueError("Package type cannot be empty")

        value_lower = value.lower().strip()
        for member in cls:
            if member.value == value_lower:
                return member

        valid_types = ", ".join(f"'{m.value}'" for m in cls)
        raise ValueError(f"Invalid package type '{value}'. Valid types are: {valid_types}")


class ValidationError(Enum):
    """Types of validation errors for APM packages."""

    MISSING_APM_YML = "missing_apm_yml"
    MISSING_APM_DIR = "missing_apm_dir"
    INVALID_YML_FORMAT = "invalid_yml_format"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_VERSION_FORMAT = "invalid_version_format"
    INVALID_DEPENDENCY_FORMAT = "invalid_dependency_format"
    EMPTY_APM_DIR = "empty_apm_dir"
    INVALID_PRIMITIVE_STRUCTURE = "invalid_primitive_structure"


class InvalidVirtualPackageExtensionError(ValueError):
    """Raised when a virtual package file has an invalid extension."""

    pass


@dataclass
class ValidationResult:
    """Result of APM package validation."""

    is_valid: bool
    errors: list[str]
    warnings: list[str]
    package: APMPackage | None = None
    package_type: PackageType | None = None  # APM_PACKAGE, CLAUDE_SKILL, or HYBRID

    def __init__(self):
        self.is_valid = True
        self.errors = []
        self.warnings = []
        self.package = None
        self.package_type = None

    def add_error(self, error: str) -> None:
        """Add a validation error."""
        self.errors.append(error)
        self.is_valid = False

    def add_warning(self, warning: str) -> None:
        """Add a validation warning."""
        self.warnings.append(warning)

    def has_issues(self) -> bool:
        """Check if there are any errors or warnings."""
        return bool(self.errors or self.warnings)

    def summary(self) -> str:
        """Get a summary of validation results."""
        if self.is_valid and not self.warnings:
            return "[+] Package is valid"
        elif self.is_valid and self.warnings:
            return f"[!] Package is valid with {len(self.warnings)} warning(s)"
        else:
            return f"[x] Package is invalid with {len(self.errors)} error(s)"


# Canonical order of the directories that mark a Claude Code marketplace
# plugin.  Tests assert this ordering on ``DetectionEvidence.plugin_dirs_present``
# so adding a new directory here is a public-API change.
_PLUGIN_DIRS: tuple[str, ...] = ("agents", "skills", "commands")


def _has_hook_json(package_path: Path) -> bool:
    """Check if the package has hook JSON files in hooks/ or .apm/hooks/."""
    for hooks_dir in [package_path / "hooks", package_path / APM_DIR / "hooks"]:
        if hooks_dir.exists() and any(hooks_dir.glob("*.json")):
            return True
    return False


def _canvas_extension_names(package_path: Path) -> list[str]:
    """Return sorted canvas bundle names declared under .apm/extensions/.

    A canvas bundle is a directory carrying an executable ``extension.mjs``
    marker (experimental, Copilot-only). Surfacing it lets validation treat a
    canvas-only package as non-empty and warn that it ships gated executable
    code. The scan is independent of the ``canvas`` experimental flag so an
    author is always informed about what their package contains.
    """
    try:
        from ..integration.canvas_integrator import CanvasIntegrator

        return [bundle.name for bundle in CanvasIntegrator.find_canvas_bundles(package_path)]
    except Exception:
        return []


@dataclass(frozen=True)
class DetectionEvidence:
    """Snapshot of the file-system signals that drove classification.

    Returned from :func:`gather_detection_evidence` and consumed by
    install-time observability (verbose detection traces, near-miss
    warnings, deploy-summary labelling).  Kept independent of
    :func:`detect_package_type` so that the classification function can
    keep its existing ``(PackageType, Optional[Path])`` return signature
    while observability code can pull richer detail on demand.
    """

    has_apm_yml: bool
    has_skill_md: bool
    has_hook_json: bool
    plugin_json_path: Path | None
    plugin_dirs_present: tuple[str, ...]
    has_claude_plugin_dir: bool = False
    nested_skill_dirs: tuple[str, ...] = ()
    has_plugin_manifest: bool = False

    @property
    def has_plugin_evidence(self) -> bool:
        """True if a real plugin manifest is present.

        Only ``plugin.json`` or ``.claude-plugin/`` directory count as
        plugin evidence.  Bare ``skills/``, ``agents/``, ``commands/``
        directories do NOT -- those are handled by the SKILL_BUNDLE
        classification path instead.
        """
        return self.has_plugin_manifest


def gather_detection_evidence(package_path: Path) -> DetectionEvidence:
    """Collect all package-type signals from a directory in one pass.

    Pure: no side-effects, no file mutations. Stat-cheap except when
    ``apm.yml`` is present without a ``.apm/`` directory, in which case it
    is parsed once to detect declared dependencies.  See
    :class:`DetectionEvidence` for the shape of the return value.

    Internally delegates to :class:`~.format_detection.PackageFormatRegistry`
    so each signal is gathered by its dedicated detector.

    Note: ``plugin_dirs_present`` is always populated (enumerating
    ``agents/``, ``skills/``, ``commands/`` if they exist) even when no
    plugin manifest is present, because observability callers use the field
    for near-miss warnings independently of classification.
    """
    from .format_detection import (
        _PLUGIN_DIRS,
        PackageFormatRegistry,
    )

    registry = PackageFormatRegistry()
    report = registry.detect(package_path)

    cp = report.claude_plugin
    sm = report.skill_md
    ay = report.apm_yml
    hj = report.hook_json

    plugin_json_path = cp.plugin_json_path if cp is not None else None
    has_claude_plugin_dir = cp.has_claude_plugin_dir if cp is not None else False
    has_plugin_manifest = cp is not None

    # Always enumerate plugin-layout dirs for observability (near-miss warnings
    # want to know about agents/skills/commands even when no manifest is present).
    plugin_dirs_present = tuple(name for name in _PLUGIN_DIRS if (package_path / name).is_dir())

    nested_skill_dirs = sm.nested_skill_dirs if sm is not None else ()

    return DetectionEvidence(
        has_apm_yml=ay is not None,
        has_skill_md=sm is not None and sm.skill_md_path is not None,
        has_hook_json=hj is not None,
        plugin_json_path=plugin_json_path,
        plugin_dirs_present=plugin_dirs_present,
        has_claude_plugin_dir=has_claude_plugin_dir,
        nested_skill_dirs=nested_skill_dirs,
        has_plugin_manifest=has_plugin_manifest,
    )


def detect_package_type(
    package_path: Path,
) -> tuple[PackageType, Path | None]:
    """Classify a package directory into a ``PackageType``.

    Thin facade over :class:`~.format_detection.PackageFormatRegistry` +
    :class:`~.format_detection.NormalizationPlanner`.  All detection logic
    lives in those classes; this function preserves the existing call-site
    signature ``(PackageType, plugin_json_path | None)``.

    Cascade order (first match wins -- implemented in NormalizationPlanner):

    1. ``MARKETPLACE_PLUGIN`` -- plugin manifest present: ``plugin.json``
       OR ``.claude-plugin/`` directory.
    2. ``HYBRID`` -- root ``SKILL.md`` AND ``apm.yml`` present.
    3. ``CLAUDE_SKILL`` -- root ``SKILL.md`` only (no ``apm.yml``).
    4. ``SKILL_BUNDLE`` -- nested ``skills/<x>/SKILL.md`` detected;
       ``apm.yml`` optional; no ``.apm/`` required.
    5. ``APM_PACKAGE`` -- ``apm.yml`` present with ``.apm/`` or declared deps.
    6. ``HOOK_PACKAGE`` -- ``hooks/*.json`` only, no other signals.
    7. ``INVALID`` -- nothing recognisable.

    Returns:
        A ``(package_type, plugin_json_path)`` tuple.  *plugin_json_path*
        is non-None only when ``MARKETPLACE_PLUGIN`` was matched via an
        actual ``plugin.json`` file (not via directory evidence alone).
    """
    from .format_detection import NormalizationPlanner, PackageFormatRegistry

    report = PackageFormatRegistry().detect(package_path)
    pkg_type, plugin_json_path = NormalizationPlanner().plan(report)
    return pkg_type, plugin_json_path


def _apm_yml_declares_dependencies(apm_yml_path: Path) -> bool:
    """Return True iff ``apm.yml`` declares at least one dependency.

    Used by ``_validate_apm_package_with_yml`` to accept a dep-only
    ``apm.yml`` (no ``.apm/`` directory) as a valid curated aggregator
    (#1094). Any non-empty ``apm`` or ``mcp`` list under ``dependencies``
    OR ``devDependencies`` qualifies. Tolerant of malformed YAML /
    missing keys: returns False on any parse problem so callers fall
    back to the legacy "missing .apm/" diagnostic instead of silently
    accepting a malformed manifest.
    """
    try:
        from ..utils.yaml_io import load_yaml

        data = load_yaml(apm_yml_path) or {}
    except Exception:
        return False
    if not isinstance(data, dict):
        return False

    def _has_listed_deps(block: object) -> bool:
        if not isinstance(block, dict):
            return False
        # Schema requires `apm` and `mcp` to be lists of strings or dicts
        # (see APMPackage._parse_dependency_dict). Non-list values, or
        # lists with no parseable entries, are malformed; treat them as
        # "no declared dependencies" so the caller falls through to the
        # legacy "missing .apm/" diagnostic instead of silently accepting
        # a malformed manifest.
        for key in ("apm", "mcp"):
            value = block.get(key)
            if isinstance(value, list) and any(isinstance(entry, (str, dict)) for entry in value):
                return True
        return False

    return _has_listed_deps(data.get("dependencies")) or _has_listed_deps(
        data.get("devDependencies")
    )


def validate_apm_package(package_path: Path) -> ValidationResult:
    """Validate that a directory contains a valid APM package or Claude Skill.

    Supports six package types:
    - APM_PACKAGE: Has apm.yml (with .apm/ for own primitives, or
      dep-only as a curated dependency aggregator -- #1094)
    - CLAUDE_SKILL: Has SKILL.md but no apm.yml (auto-generates apm.yml)
    - HOOK_PACKAGE: Has hooks/*.json but no apm.yml or SKILL.md
    - MARKETPLACE_PLUGIN: Has plugin.json or .claude-plugin/ (synthesizes apm.yml)
    - HYBRID: Has both apm.yml and root SKILL.md
    - SKILL_BUNDLE: Has skills/<name>/SKILL.md, apm.yml optional

    Args:
        package_path: Path to the directory to validate

    Returns:
        ValidationResult: Validation results with any errors/warnings
    """
    result = ValidationResult()

    # Check if directory exists
    if not package_path.exists():
        result.add_error(f"Package directory does not exist: {package_path}")
        return result

    if not package_path.is_dir():
        result.add_error(f"Package path is not a directory: {package_path}")
        return result

    # Detect package type
    pkg_type, plugin_json_path = detect_package_type(package_path)
    result.package_type = pkg_type

    if pkg_type == PackageType.INVALID:
        _add_invalid_package_error(package_path, result)
        return result

    return _dispatch_package_validation(package_path, plugin_json_path, result)


def _add_invalid_package_error(package_path: Path, result: ValidationResult) -> None:
    """Record the appropriate error for an INVALID package directory.

    Two sub-cases of INVALID:
    1. apm.yml present but no .apm/ directory (or .apm is a file)
    2. Nothing recognizable at all
    """
    apm_yml_path = package_path / APM_YML_FILENAME
    if apm_yml_path.exists():
        apm_path = package_path / APM_DIR
        if apm_path.exists() and not apm_path.is_dir():
            result.add_error(".apm must be a directory")
        else:
            result.add_error(
                f"Not a valid APM package: {package_path.name} has apm.yml but "
                "is missing the required .apm/ directory. "
                "Add .apm/ with primitives (instructions, skills, etc.), "
                "declare dependencies in apm.yml (curated aggregator), "
                "or add skills/<name>/SKILL.md for a skill bundle."
            )
    else:
        result.add_error(
            f"Not a valid APM package: no apm.yml, SKILL.md, hooks, or "
            f"plugin structure found in {package_path.name}. "
            "Ensure the package has SKILL.md (skill bundle), "
            "apm.yml + .apm/ (APM package), or plugin.json (Claude plugin) "
            "at its root."
        )


def _dispatch_package_validation(
    package_path: Path, plugin_json_path: Path | None, result: ValidationResult
) -> ValidationResult:
    """Route a non-INVALID package to its type-specific validator.

    Validators are referenced via the module-level names re-exported from
    ``_validation_rules`` (see the imports at the top of this module) so that
    tests patching ``apm_cli.models.validation._validate_*`` intercept the
    dispatch. Do NOT re-import them locally here -- a local import rebinds the
    names to the originals and defeats that patch contract.
    """
    # Handle hook-only packages (no apm.yml or SKILL.md)
    if result.package_type == PackageType.HOOK_PACKAGE:
        return _validate_hook_package(package_path, result)

    # Handle Claude Skills (no apm.yml) - auto-generate minimal apm.yml
    skill_md_path = package_path / SKILL_MD_FILENAME
    if result.package_type == PackageType.CLAUDE_SKILL:
        return _validate_claude_skill(package_path, skill_md_path, result)

    # Handle Marketplace Plugins (no apm.yml) - synthesize apm.yml from plugin.json
    if result.package_type == PackageType.MARKETPLACE_PLUGIN:
        return _validate_marketplace_plugin(package_path, plugin_json_path, result)

    # Handle Skill Bundles (nested skills/<name>/SKILL.md)
    if result.package_type == PackageType.SKILL_BUNDLE:
        return _validate_skill_bundle(package_path, result)

    # Standard APM package or HYBRID validation (has apm.yml)
    apm_yml_path = package_path / APM_YML_FILENAME

    # HYBRID packages: if .apm/ exists, fall through to standard validation
    # (back-compat for packages that ship both .apm/ primitives AND SKILL.md).
    # Otherwise validate as a skill bundle with apm.yml metadata.
    if result.package_type == PackageType.HYBRID:
        return _validate_hybrid_package(package_path, apm_yml_path, result)

    return _validate_apm_package_with_yml(package_path, apm_yml_path, result)
