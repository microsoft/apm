"""Validation logic and type enums for APM packages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple  # noqa: F401, UP035

from ..constants import APM_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME

if TYPE_CHECKING:
    from .apm_package import APMPackage


class PackageType(Enum):
    """Types of packages that APM can install.

    This enum is used internally to classify packages based on their content
    (presence of apm.yml, SKILL.md, hooks/, plugin.json, etc.).
    """

    APM_PACKAGE = "apm_package"  # Has apm.yml + .apm/
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

    Pure: no side-effects, no file mutations.  Cheap (a handful of stat
    calls).  See :class:`DetectionEvidence` for the shape of the return
    value.
    """
    from ..utils.helpers import find_plugin_json

    plugin_dirs_present = tuple(name for name in _PLUGIN_DIRS if (package_path / name).is_dir())
    plugin_json_path = find_plugin_json(package_path)
    has_claude_plugin_dir = (package_path / ".claude-plugin").is_dir()

    # Plugin manifest = plugin.json OR .claude-plugin/ directory.
    has_plugin_manifest = plugin_json_path is not None or has_claude_plugin_dir

    # Nested skill dirs: directories under skills/ that contain a SKILL.md.
    nested_skill_dirs: tuple[str, ...] = ()
    skills_dir = package_path / "skills"
    if skills_dir.is_dir():
        nested_skill_dirs = tuple(
            d.name
            for d in sorted(skills_dir.iterdir())
            if d.is_dir() and (d / SKILL_MD_FILENAME).exists()
        )

    return DetectionEvidence(
        has_apm_yml=(package_path / APM_YML_FILENAME).exists(),
        has_skill_md=(package_path / SKILL_MD_FILENAME).exists(),
        has_hook_json=_has_hook_json(package_path),
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

    Single source of truth for the detection cascade.  Pure: no
    side-effects, no file mutations.

    Cascade order (first match wins):

    1. ``MARKETPLACE_PLUGIN`` -- plugin manifest present: ``plugin.json``
       OR ``.claude-plugin/`` directory.  This is the strictest signal
       (explicit plugin packaging intent).
    2. ``HYBRID`` -- root ``SKILL.md`` AND ``apm.yml`` present.
    3. ``CLAUDE_SKILL`` -- root ``SKILL.md`` only (no ``apm.yml``).
    4. ``SKILL_BUNDLE`` -- nested ``skills/<x>/SKILL.md`` detected;
       ``apm.yml`` optional; no ``.apm/`` required.
    5. ``APM_PACKAGE`` -- ``apm.yml`` AND ``.apm/`` directory present.
    6. ``INVALID`` -- ``apm.yml`` present but no ``.apm/`` and no nested
       skills (helpful error: author likely needs to add .apm/).
    7. ``HOOK_PACKAGE`` -- ``hooks/*.json`` only, no other signals.
    8. ``INVALID`` -- nothing recognisable.

    Returns:
        A ``(package_type, plugin_json_path)`` tuple.  *plugin_json_path*
        is non-None only when ``MARKETPLACE_PLUGIN`` was matched via an
        actual ``plugin.json`` file (not via directory evidence alone).
    """
    evidence = gather_detection_evidence(package_path)

    # 1. Plugin manifest present -> MARKETPLACE_PLUGIN
    if evidence.has_plugin_manifest:
        return PackageType.MARKETPLACE_PLUGIN, evidence.plugin_json_path

    # 2. Root SKILL.md + apm.yml -> HYBRID
    if evidence.has_apm_yml and evidence.has_skill_md:
        return PackageType.HYBRID, None

    # 3. Root SKILL.md only -> CLAUDE_SKILL
    if evidence.has_skill_md:
        return PackageType.CLAUDE_SKILL, None

    # 4. Nested skills/<x>/SKILL.md -> SKILL_BUNDLE (apm.yml optional)
    if evidence.nested_skill_dirs:
        return PackageType.SKILL_BUNDLE, None

    # 5. apm.yml + .apm/ -> APM_PACKAGE
    if evidence.has_apm_yml:
        apm_dir = package_path / APM_DIR
        if apm_dir.is_dir():
            return PackageType.APM_PACKAGE, None
        # 6. apm.yml only (no .apm/, no nested skills) -> INVALID
        return PackageType.INVALID, None

    # 7. hooks/*.json only -> HOOK_PACKAGE
    if evidence.has_hook_json:
        return PackageType.HOOK_PACKAGE, None

    # 8. Nothing recognisable -> INVALID
    return PackageType.INVALID, None


def validate_apm_package(package_path: Path) -> ValidationResult:
    """Validate that a directory contains a valid APM package or Claude Skill.

    Supports six package types:
    - APM_PACKAGE: Has apm.yml and .apm/ directory
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
        # Two sub-cases of INVALID:
        # 1. apm.yml present but no .apm/ directory (or .apm is a file)
        # 2. Nothing recognizable at all
        apm_yml_path = package_path / APM_YML_FILENAME
        if apm_yml_path.exists():
            apm_path = package_path / APM_DIR
            if apm_path.exists() and not apm_path.is_dir():
                result.add_error(".apm must be a directory")
            else:
                result.add_error(
                    f"Not a valid APM package: {package_path.name} has apm.yml but "
                    "is missing the required .apm/ directory. "
                    "Add .apm/ with primitives (instructions, skills, etc.) "
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
        return result

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


def _validate_hook_package(package_path: Path, result: ValidationResult) -> ValidationResult:
    """Validate a hook-only package and create APMPackage from its metadata.

    A hook package has hooks/*.json (or .apm/hooks/*.json) defining hook
    handlers per the Claude Code hooks specification, but no apm.yml or SKILL.md.

    Args:
        package_path: Path to the package directory
        result: ValidationResult to populate

    Returns:
        ValidationResult: Updated validation result
    """
    from .apm_package import APMPackage

    package_name = package_path.name

    # Create APMPackage from directory name
    package = APMPackage(
        name=package_name,
        version="1.0.0",
        description=f"Hook package: {package_name}",
        package_path=package_path,
        type=PackageContentType.HYBRID,
    )
    result.package = package

    return result


def _validate_claude_skill(
    package_path: Path, skill_md_path: Path, result: ValidationResult
) -> ValidationResult:
    """Validate a Claude Skill and create APMPackage directly from SKILL.md metadata.

    Args:
        package_path: Path to the package directory
        skill_md_path: Path to SKILL.md
        result: ValidationResult to populate

    Returns:
        ValidationResult: Updated validation result
    """
    import frontmatter

    from .apm_package import APMPackage

    try:
        # Parse SKILL.md to extract metadata
        with open(skill_md_path, encoding="utf-8") as f:
            post = frontmatter.load(f)

        skill_name = post.metadata.get("name", package_path.name)
        skill_description = post.metadata.get("description", f"Claude Skill: {skill_name}")
        skill_license = post.metadata.get("license")

        # Create APMPackage directly from SKILL.md metadata - no file generation needed
        package = APMPackage(
            name=skill_name,
            version="1.0.0",
            description=skill_description,
            license=skill_license,
            package_path=package_path,
            type=PackageContentType.SKILL,
        )
        result.package = package

    except Exception as e:
        result.add_error(f"Failed to process {SKILL_MD_FILENAME}: {e}")
        return result

    return result


def _validate_skill_bundle(package_path: Path, result: ValidationResult) -> ValidationResult:
    """Validate a SKILL_BUNDLE package (nested skills/<name>/SKILL.md).

    For each ``skills/<name>/`` with a SKILL.md:
    - Validate path segments (no traversal).
    - Ensure resolved path is within package_path/skills.
    - Validate frontmatter: name field equals ``<name>``, description present,
      ASCII-only content.
    - Collect errors with the ``skills/<name>/SKILL.md`` path.

    apm.yml is OPTIONAL: if present, parse + merge metadata; if absent,
    synthesize APMPackage from the bundle (name from directory, version 0.0.0).

    Args:
        package_path: Path to the package directory
        result: ValidationResult to populate

    Returns:
        ValidationResult: Updated validation result
    """
    import frontmatter as _frontmatter

    from ..utils.path_security import ensure_path_within, validate_path_segments
    from .apm_package import APMPackage

    skills_dir = package_path / "skills"
    apm_yml_path = package_path / APM_YML_FILENAME

    # Enumerate nested skill dirs
    nested_dirs = [
        d for d in sorted(skills_dir.iterdir()) if d.is_dir() and (d / SKILL_MD_FILENAME).exists()
    ]

    if not nested_dirs:
        result.add_error(
            f"SKILL_BUNDLE detected but no valid skills/<name>/SKILL.md found "
            f"in {package_path.name}/skills/"
        )
        return result

    skill_names: list[str] = []
    for skill_dir in nested_dirs:
        name = skill_dir.name

        # Path safety: reject traversal in directory name
        try:
            validate_path_segments(name, context=f"skills/{name}")
        except ValueError as e:
            result.add_error(str(e))
            continue

        # Path safety: ensure resolved SKILL.md is within skills/
        skill_md_path = skill_dir / SKILL_MD_FILENAME
        try:
            ensure_path_within(skill_md_path, skills_dir)
        except ValueError as e:
            result.add_error(str(e))
            continue

        # Validate frontmatter
        try:
            with open(skill_md_path, encoding="utf-8") as f:
                post = _frontmatter.load(f)
        except Exception as e:
            result.add_error(f"skills/{name}/SKILL.md: failed to parse frontmatter: {e}")
            continue

        # Name field must equal directory name (if present)
        fm_name = post.metadata.get("name", "")
        if fm_name and fm_name != name:
            result.add_warning(
                f"skills/{name}/SKILL.md: frontmatter name '{fm_name}' "
                f"does not match directory name '{name}' "
                f"(APM will use directory name '{name}' for deployment)"
            )

        # Description must be present
        fm_desc = post.metadata.get("description", "")
        if not fm_desc:
            result.add_warning(f"skills/{name}/SKILL.md: missing 'description' in frontmatter")

        # ASCII-only check on frontmatter values (warn only -- many real-world
        # packages use non-ASCII descriptions, e.g. i18n skill repos)
        for key, val in post.metadata.items():
            if isinstance(val, str) and not val.isascii():
                result.add_warning(
                    f"skills/{name}/SKILL.md: frontmatter field '{key}' "
                    f"contains non-ASCII characters"
                )
                break

        skill_names.append(name)

    if not skill_names and result.errors:
        # All skills failed validation
        return result

    # Build APMPackage: use apm.yml if present, otherwise synthesize
    if apm_yml_path.exists():
        try:
            package = APMPackage.from_apm_yml(apm_yml_path)
        except (ValueError, FileNotFoundError) as e:
            result.add_error(f"Invalid apm.yml: {e}")
            return result
    else:
        # Synthesize minimal APMPackage from bundle directory
        package = APMPackage(
            name=package_path.name,
            version="0.0.0",
            description=f"Skill bundle: {package_path.name}",
            package_path=package_path,
            type=PackageContentType.SKILL,
        )

    result.package = package
    return result


def _validate_hybrid_package(
    package_path: Path, apm_yml_path: Path, result: ValidationResult
) -> ValidationResult:
    """Validate a HYBRID package (apm.yml + SKILL.md).

    Two sub-cases:

    1. ``.apm/`` directory present -- fall through to the standard
       ``_validate_apm_package_with_yml`` path for full back-compat.
    2. No ``.apm/`` -- treat as a *skill bundle* whose metadata comes from
       ``apm.yml`` (authoritative for name/version/license/deps) and whose
       runtime behavior is driven by ``SKILL.md``.  This is the Genesis
       layout: ``apm.yml`` + ``SKILL.md`` + optional sub-directories at
       repo root, no ``.apm/``.

    Args:
        package_path: Path to the package directory
        apm_yml_path: Path to apm.yml
        result: ValidationResult to populate

    Returns:
        ValidationResult: Updated validation result
    """
    # Back-compat: if .apm/ exists, the author intends independent primitives.
    apm_dir = package_path / APM_DIR
    if apm_dir.exists() and apm_dir.is_dir():
        return _validate_apm_package_with_yml(package_path, apm_yml_path, result)

    # --- Skill-bundle path (no .apm/) ---
    from .apm_package import APMPackage

    # Parse apm.yml -- authoritative for APM-owned fields.
    try:
        package = APMPackage.from_apm_yml(apm_yml_path)
    except (ValueError, FileNotFoundError) as e:
        result.add_error(f"Invalid apm.yml: {e}")
        return result

    # Require SKILL.md present and minimally readable.
    skill_md_path = package_path / SKILL_MD_FILENAME
    if not skill_md_path.exists():
        result.add_error(f"HYBRID package missing {SKILL_MD_FILENAME}")
        return result

    try:
        import frontmatter

        with open(skill_md_path, encoding="utf-8") as f:
            frontmatter.load(f)  # Parse only to surface malformed frontmatter.

        # Metadata model for HYBRID packages: apm.yml.description and
        # SKILL.md frontmatter description are INDEPENDENT fields with
        # different consumers and MUST NOT be merged.
        #
        #   * apm.yml.description -> human tagline rendered by `apm view`,
        #     `apm search`, `apm deps list`, marketplace/registry indexes.
        #   * SKILL.md description -> agent-runtime invocation matcher
        #     (per agentskills.io), consumed verbatim by Claude/Copilot/etc.
        #     APM never reads or mutates this field; the file is copied
        #     byte-for-byte into <target>/skills/<name>/ at integrate time.
        #
        # Authors who ship a HYBRID package are expected to populate both
        # descriptions independently. The pack-time check in
        # `apm_cli.bundle.packer` warns when apm.yml.description is missing
        # so the human-facing surfaces (search/listings) do not degrade
        # silently while the agent runtime keeps working.

    except Exception as e:
        result.add_warning(f"Could not parse {SKILL_MD_FILENAME} frontmatter: {e}")

    result.package = package
    # package_type already set to HYBRID by the caller
    return result


def _validate_marketplace_plugin(
    package_path: Path, plugin_json_path: Path | None, result: ValidationResult
) -> ValidationResult:
    """Validate a Claude plugin and synthesize apm.yml.

    plugin.json is **optional** per the spec.  When present it provides
    metadata (name, version, description ...).  When absent the plugin name is
    derived from the directory name and all other fields default gracefully.

    Args:
        package_path: Path to the package directory
        plugin_json_path: Path to plugin.json if found, or None
        result: ValidationResult to populate

    Returns:
        ValidationResult: Updated validation result with MARKETPLACE_PLUGIN type
    """
    from ..deps.plugin_parser import normalize_plugin_directory
    from .apm_package import APMPackage

    try:
        # Normalize the plugin directory; plugin.json is optional metadata
        apm_yml_path = normalize_plugin_directory(package_path, plugin_json_path)

        # Load the synthesized apm.yml
        package = APMPackage.from_apm_yml(apm_yml_path)
        result.package = package
        result.package_type = PackageType.MARKETPLACE_PLUGIN

    except Exception as e:
        result.add_error(f"Failed to process Claude plugin: {e}")
        return result

    return result


def _validate_apm_package_with_yml(
    package_path: Path, apm_yml_path: Path, result: ValidationResult
) -> ValidationResult:
    """Validate a standard APM package with apm.yml.

    Args:
        package_path: Path to the package directory
        apm_yml_path: Path to apm.yml
        result: ValidationResult to populate

    Returns:
        ValidationResult: Updated validation result
    """
    from .apm_package import APMPackage

    # Try to parse apm.yml
    try:
        package = APMPackage.from_apm_yml(apm_yml_path)
        result.package = package
    except (ValueError, FileNotFoundError) as e:
        result.add_error(f"Invalid apm.yml: {e}")
        return result

    # Check for .apm directory
    apm_dir = package_path / APM_DIR
    if not apm_dir.exists():
        result.add_error(
            f"Missing required directory: {APM_DIR}/ -- "
            "an APM package with apm.yml needs a .apm/ directory containing "
            "primitives. Alternatively, add a SKILL.md to make this a skill "
            "bundle or hybrid package."
        )
        return result

    if not apm_dir.is_dir():
        result.add_error(f"{APM_DIR} must be a directory")
        return result

    # Check if .apm directory has any content
    primitive_types = ["instructions", "chatmodes", "contexts", "prompts"]
    has_primitives = False

    for primitive_type in primitive_types:
        primitive_dir = apm_dir / primitive_type
        if primitive_dir.exists() and primitive_dir.is_dir():
            # Check if directory has any markdown files
            md_files = list(primitive_dir.glob("*.md"))
            if md_files:
                has_primitives = True
                # Validate each primitive file has basic structure
                for md_file in md_files:
                    try:
                        content = md_file.read_text(encoding="utf-8")
                        if not content.strip():
                            result.add_warning(
                                f"Empty primitive file: {md_file.relative_to(package_path)}"
                            )
                    except Exception as e:
                        result.add_warning(
                            f"Could not read primitive file {md_file.relative_to(package_path)}: {e}"
                        )

    # Also check for hooks (JSON files in .apm/hooks/ or hooks/)
    if not has_primitives:
        has_primitives = _has_hook_json(package_path)

    if not has_primitives:
        result.add_warning(f"No primitive files found in {APM_DIR}/ directory")

    # Version format validation (basic semver check)
    if package and package.version is not None:
        # Defensive cast in case YAML parsed a numeric like 1 or 1.0
        version_str = str(package.version).strip()
        if not re.match(r"^\d+\.\d+\.\d+", version_str):
            result.add_warning(
                f"Version '{version_str}' doesn't follow semantic versioning (x.y.z)"
            )

    return result
