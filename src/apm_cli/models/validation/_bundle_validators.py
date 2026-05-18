"""Bundle / skill-bundle / hybrid / marketplace-plugin validators."""

from __future__ import annotations

import re
from pathlib import Path

from ...constants import APM_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME
from ._detection import _apm_yml_declares_dependencies, _has_hook_json
from ._types import PackageContentType, PackageType, ValidationResult


def _validate_skill_frontmatter(skill_md_path: Path, name: str, result: ValidationResult) -> None:
    """Validate a single skill's frontmatter.

    Args:
        skill_md_path: Path to SKILL.md
        name: Expected skill name from directory
        result: ValidationResult to update
    """
    import frontmatter as _frontmatter

    try:
        with open(skill_md_path, encoding="utf-8") as f:
            post = _frontmatter.load(f)
    except Exception as e:
        result.add_error(f"skills/{name}/SKILL.md: failed to parse frontmatter: {e}")
        return

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
                f"skills/{name}/SKILL.md: frontmatter field '{key}' contains non-ASCII characters"
            )
            break


def _validate_single_skill(
    skill_dir: Path, skills_dir: Path, result: ValidationResult
) -> str | None:
    """Validate a single skill directory and return its name if valid.

    Args:
        skill_dir: Path to the skill directory
        skills_dir: Path to the parent skills directory
        result: ValidationResult to update

    Returns:
        Skill name if valid, None otherwise
    """
    from ...utils.path_security import ensure_path_within, validate_path_segments

    name = skill_dir.name

    # Path safety: reject traversal in directory name
    try:
        validate_path_segments(name, context=f"skills/{name}")
    except ValueError as e:
        result.add_error(str(e))
        return None

    # Path safety: ensure resolved SKILL.md is within skills/
    skill_md_path = skill_dir / SKILL_MD_FILENAME
    try:
        ensure_path_within(skill_md_path, skills_dir)
    except ValueError as e:
        result.add_error(str(e))
        return None

    # Validate frontmatter
    _validate_skill_frontmatter(skill_md_path, name, result)

    # Return name even if frontmatter had warnings (skill is still valid)
    return name


def _validate_skill_bundle(package_path: Path, result: ValidationResult) -> ValidationResult:
    """Validate a SKILL_BUNDLE package (nested skills/<name>/SKILL.md).

    For each ``skills/<name>/`` with a SKILL.md:
    - Validate path segments (no traversal).
    - Ensure resolved path is within package_path/skills.
    - Validate frontmatter: name field equals ``<name>``, description present,
      ASCII-only content.
    - Collect errors with the ``skills/<name>/SKILL.md`` path.

    apm.yml is OPTIONAL: if present, parse + merge metadata; if absent,
    synthesise APMPackage from the bundle (name from directory, version 0.0.0).

    Args:
        package_path: Path to the package directory
        result: ValidationResult to populate

    Returns:
        ValidationResult: Updated validation result
    """
    from ..apm_package import APMPackage

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
        name = _validate_single_skill(skill_dir, skills_dir, result)
        if name:
            skill_names.append(name)

    if not skill_names and result.errors:
        # All skills failed validation
        return result

    # Build APMPackage: use apm.yml if present, otherwise synthesise
    if apm_yml_path.exists():
        try:
            package = APMPackage.from_apm_yml(apm_yml_path)
        except (ValueError, FileNotFoundError) as e:
            result.add_error(f"Invalid apm.yml: {e}")
            return result
    else:
        # Synthesise minimal APMPackage from bundle directory
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
       runtime behaviour is driven by ``SKILL.md``.  This is the Genesis
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
    from ..apm_package import APMPackage

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
    """Validate a Claude plugin and synthesise apm.yml.

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
    from ...deps.plugin_parser import normalize_plugin_directory
    from ..apm_package import APMPackage

    try:
        # Normalise the plugin directory; plugin.json is optional metadata
        apm_yml_path = normalize_plugin_directory(package_path, plugin_json_path)

        # Load the synthesised apm.yml
        package = APMPackage.from_apm_yml(apm_yml_path)
        result.package = package
        result.package_type = PackageType.MARKETPLACE_PLUGIN

    except Exception as e:
        result.add_error(f"Failed to process Claude plugin: {e}")
        return result

    return result


def _check_primitive_files(package_path: Path, apm_dir: Path, result: ValidationResult) -> bool:
    """Check for primitive files in .apm directory.

    Args:
        package_path: Path to package directory
        apm_dir: Path to .apm directory
        result: ValidationResult to update

    Returns:
        True if primitives found, False otherwise
    """
    primitive_types = ["instructions", "chatmodes", "contexts", "prompts"]

    for primitive_type in primitive_types:
        primitive_dir = apm_dir / primitive_type
        if primitive_dir.exists() and primitive_dir.is_dir():
            # Check if directory has any markdown files
            md_files = list(primitive_dir.glob("*.md"))
            if md_files:
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
                            f"Could not read primitive file "
                            f"{md_file.relative_to(package_path)}: {e}"
                        )
                return True

    return False


def _validate_version_format(package, result: ValidationResult) -> None:
    """Validate package version format.

    Args:
        package: APMPackage instance
        result: ValidationResult to update
    """
    if package and package.version is not None:
        # Defensive cast in case YAML parsed a numeric like 1 or 1.0
        version_str = str(package.version).strip()
        if not re.match(r"^\d+\.\d+\.\d+", version_str):
            result.add_warning(
                f"Version '{version_str}' doesn't follow semantic versioning (x.y.z)"
            )


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
    from ..apm_package import APMPackage

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
    has_primitives = _check_primitive_files(package_path, apm_dir, result)

    # Also check for hooks (JSON files in .apm/hooks/ or hooks/)
    if not has_primitives:
        has_primitives = _has_hook_json(package_path)

    if not has_primitives:
        result.add_warning(f"No primitive files found in {APM_DIR}/ directory")

    # Version format validation (basic semver check)
    _validate_version_format(package, result)

    return result
