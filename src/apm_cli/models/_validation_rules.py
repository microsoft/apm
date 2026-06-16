"""Leaf validator functions for each APM package type.

Extracted from ``validation.py`` to keep that module under the file-length
gate.  Import the public symbols via ``apm_cli.models.validation`` (which
reaches them through a lazy import in ``_dispatch_package_validation``) rather
than directly from this module.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..constants import APM_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME

# Runtime imports of names defined in validation.py are done lazily (inside
# each function) to avoid a circular import.  The pattern mirrors the existing
# lazy ``from .apm_package import APMPackage`` calls already present in these
# functions.


def _validate_hook_package(package_path: Path, result: Any) -> Any:
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
    from .validation import PackageContentType

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


def _validate_claude_skill(package_path: Path, skill_md_path: Path, result: Any) -> Any:
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
    from .validation import PackageContentType

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


def _validate_skill_bundle(package_path: Path, result: Any) -> Any:
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
    from .validation import PackageContentType

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


def _validate_hybrid_package(package_path: Path, apm_yml_path: Path, result: Any) -> Any:
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
        # Route through validation module so patch sites on
        # ``apm_cli.models.validation._validate_apm_package_with_yml`` intercept
        # this call correctly (the function lives here but tests patch the
        # re-export on the validation namespace).
        from .validation import _validate_apm_package_with_yml as _std_validate

        return _std_validate(package_path, apm_yml_path, result)

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
    package_path: Path, plugin_json_path: Path | None, result: Any
) -> Any:
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
    from .validation import PackageType

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


def _validate_apm_package_with_yml(package_path: Path, apm_yml_path: Path, result: Any) -> Any:
    """Validate a standard APM package with apm.yml.

    Args:
        package_path: Path to the package directory
        apm_yml_path: Path to apm.yml
        result: ValidationResult to populate

    Returns:
        ValidationResult: Updated validation result
    """
    from .apm_package import APMPackage
    from .validation import _apm_yml_declares_dependencies, _canvas_extension_names, _has_hook_json

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
        # Dep-only packages (apm.yml with dependencies, no .apm/) are valid
        # curated aggregators (#1094). Only fail if there are no dependencies
        # either -- that's the original "unfinished package" diagnostic.
        if _apm_yml_declares_dependencies(apm_yml_path):
            return result
        result.add_error(
            f"Missing required directory: {APM_DIR}/ -- "
            "an APM package with apm.yml needs either a .apm/ directory "
            "containing primitives, or dependencies declared in apm.yml. "
            "Alternatively, add a SKILL.md to make this a skill bundle."
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

    # Canvas extensions: experimental, Copilot-only bundles that ship an
    # executable extension.mjs. They count as primitives (so a canvas-only
    # package is not mis-flagged as empty) and earn an explicit warning that
    # they are gated executable code.
    canvas_names = _canvas_extension_names(package_path)
    if canvas_names:
        has_primitives = True
        result.add_warning(
            "Canvas extension(s) found (experimental, Copilot-only): "
            f"{', '.join(canvas_names)}. These ship executable extension.mjs "
            "code; consumers must enable the 'canvas' experimental flag, and "
            "dependents must pass --trust-canvas-extensions to install them."
        )

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
