"""Discovery functionality for primitive files."""

import logging
import os
from pathlib import Path

from ..deps.lockfile import LockFile
from ..models.apm_package import APMPackage
from ..utils.exclude import should_exclude, validate_exclude_patterns
from ._dependency_order import get_dependency_declaration_order as _get_dependency_declaration_order
from ._discovery_walk import (
    _exclude_matches_dir,
    _glob_match,
    _should_skip_directory,
    find_primitive_files,
)
from .models import PrimitiveCollection
from .parser import parse_primitive_file, parse_skill_file

logger = logging.getLogger(__name__)


def _is_readable(file_path: Path) -> bool:
    """Check if a file is readable via this module's ``open`` binding."""
    try:
        with open(file_path, encoding="utf-8") as file_obj:
            file_obj.read(1)
        return True
    except (PermissionError, UnicodeDecodeError, OSError):
        return False


def get_dependency_declaration_order(base_dir: str) -> list[str]:
    """Preserve legacy patch targets for dependency-order discovery."""
    return _get_dependency_declaration_order(
        base_dir, apm_package_cls=APMPackage, lockfile_cls=LockFile
    )


# Common primitive patterns for local discovery (with recursive search)
LOCAL_PRIMITIVE_PATTERNS: dict[str, list[str]] = {
    "chatmode": [
        # New standard (.agent.md)
        "**/.apm/agents/*.agent.md",
        "**/.github/agents/*.agent.md",
        "**/*.agent.md",  # Generic .agent.md files
        # Legacy support (.chatmode.md)
        "**/.apm/chatmodes/*.chatmode.md",
        "**/.github/chatmodes/*.chatmode.md",
        "**/*.chatmode.md",  # Generic .chatmode.md files
    ],
    "instruction": [
        "**/.apm/instructions/*.instructions.md",
        "**/.github/instructions/*.instructions.md",
        "**/*.instructions.md",  # Generic .instructions.md files
    ],
    "context": [
        "**/.apm/context/*.context.md",
        "**/.apm/memory/*.memory.md",  # APM memory convention
        "**/.github/context/*.context.md",
        "**/.github/memory/*.memory.md",  # VSCode compatibility
        "**/*.context.md",  # Generic .context.md files
        "**/*.memory.md",  # Generic .memory.md files
    ],
}

# Dependency primitive patterns (for .apm directory within dependencies)
DEPENDENCY_PRIMITIVE_PATTERNS: dict[str, list[str]] = {
    "chatmode": [
        "agents/*.agent.md",  # New standard
        "chatmodes/*.chatmode.md",  # Legacy
    ],
    "instruction": ["instructions/*.instructions.md"],
    "context": ["context/*.context.md", "memory/*.memory.md"],
}

# Dependency primitive patterns for .github directory within dependencies.
# Some packages store primitives in .github/ instead of (or in addition to) .apm/.
DEPENDENCY_GITHUB_PRIMITIVE_PATTERNS: dict[str, list[str]] = {
    "chatmode": [
        "agents/*.agent.md",
        "chatmodes/*.chatmode.md",
    ],
    "instruction": ["instructions/*.instructions.md"],
    "context": [
        "context/*.context.md",
        "memory/*.memory.md",
    ],
}


def discover_primitives(
    base_dir: str = ".",
    exclude_patterns: list[str] | None = None,
) -> PrimitiveCollection:
    """Find all APM primitive files in the project.

    Searches for .chatmode.md, .instructions.md, .context.md, .memory.md files
    in both .apm/ and .github/ directory structures, plus SKILL.md at root.

    Args:
        base_dir (str): Base directory to search in. Defaults to current directory.
        exclude_patterns (Optional[List[str]]): Glob patterns for paths to exclude.

    Returns:
        PrimitiveCollection: Collection of discovered and parsed primitives.
    """
    collection = PrimitiveCollection()
    Path(base_dir)
    safe_patterns = validate_exclude_patterns(exclude_patterns)

    # Find and parse files for each primitive type
    for _primitive_type, patterns in LOCAL_PRIMITIVE_PATTERNS.items():
        files = find_primitive_files(base_dir, patterns, exclude_patterns=safe_patterns)

        for file_path in files:
            try:
                primitive = parse_primitive_file(file_path, source="local")
                collection.add_primitive(primitive)
            except Exception as e:
                print(f"Warning: Failed to parse {file_path}: {e}")

    # Discover SKILL.md at project root
    _discover_local_skill(base_dir, collection, exclude_patterns=safe_patterns)

    return collection


def discover_primitives_with_dependencies(
    base_dir: str = ".",
    exclude_patterns: list[str] | None = None,
) -> PrimitiveCollection:
    """Enhanced primitive discovery including dependency sources.

    Priority Order:
    1. Local .apm/ (highest priority - always wins)
    2. Dependencies in declaration order (first declared wins)
    3. Plugins (lowest priority)

    Args:
        base_dir (str): Base directory to search in. Defaults to current directory.
        exclude_patterns (Optional[List[str]]): Glob patterns for paths to exclude.

    Returns:
        PrimitiveCollection: Collection of discovered and parsed primitives with source tracking.
    """
    collection = PrimitiveCollection()
    safe_patterns = validate_exclude_patterns(exclude_patterns)

    # Phase 1: Local primitives (highest priority)
    scan_local_primitives(base_dir, collection, exclude_patterns=safe_patterns)

    # Phase 1b: Local SKILL.md
    _discover_local_skill(base_dir, collection, exclude_patterns=safe_patterns)

    # Phase 2: Dependency primitives (lower priority, with conflict detection)
    # Plugins are normalized into standard APM packages during install
    # (apm.yml + .apm/ are synthesized), so scan_dependency_primitives handles them.
    scan_dependency_primitives(base_dir, collection)

    return collection


def scan_local_primitives(
    base_dir: str,
    collection: PrimitiveCollection,
    exclude_patterns: list[str] | None = None,
) -> None:
    """Scan local .apm/ directory for primitives.

    Args:
        base_dir (str): Base directory to search in.
        collection (PrimitiveCollection): Collection to add primitives to.
        exclude_patterns (Optional[List[str]]): Pre-validated exclude patterns.
    """
    # Find and parse files for each primitive type
    for _primitive_type, patterns in LOCAL_PRIMITIVE_PATTERNS.items():
        files = find_primitive_files(base_dir, patterns, exclude_patterns=exclude_patterns)

        # Filter out files from apm_modules to avoid conflicts with dependency scanning
        local_files = []
        base_path = Path(base_dir)
        apm_modules_path = base_path / "apm_modules"

        for file_path in files:
            # Only include files that are NOT in apm_modules directory
            if _is_under_directory(file_path, apm_modules_path):
                continue
            local_files.append(file_path)

        for file_path in local_files:
            try:
                primitive = parse_primitive_file(file_path, source="local")
                collection.add_primitive(primitive)
            except Exception as e:
                print(f"Warning: Failed to parse local primitive {file_path}: {e}")


def _is_under_directory(file_path: Path, directory: Path) -> bool:
    """Check if a file path is under a specific directory.

    Args:
        file_path (Path): Path to check.
        directory (Path): Directory to check against.

    Returns:
        bool: True if file_path is under directory, False otherwise.
    """
    try:
        file_path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def scan_dependency_primitives(base_dir: str, collection: PrimitiveCollection) -> None:
    """Scan all dependencies in apm_modules/ with priority handling.

    Args:
        base_dir (str): Base directory to search in.
        collection (PrimitiveCollection): Collection to add primitives to.
    """
    apm_modules_path = Path(base_dir) / "apm_modules"
    if not apm_modules_path.exists():
        return

    # Get dependency declaration order from apm.yml
    dependency_order = get_dependency_declaration_order(base_dir)

    # Process dependencies in declaration order
    for dep_name in dependency_order:
        # Join all path parts to handle variable-length paths:
        # GitHub: "owner/repo" (2 parts)
        # Azure DevOps: "org/project/repo" (3 parts)
        # Virtual subdirectory: "owner/repo/subdir" or deeper (3+ parts)
        parts = dep_name.split("/")
        dep_path = apm_modules_path.joinpath(*parts)

        if dep_path.exists() and dep_path.is_dir():
            scan_directory_with_source(dep_path, collection, source=f"dependency:{dep_name}")


def _matches_any_pattern(rel_path: str, patterns: list[str]) -> bool:
    """Return ``True`` if *rel_path* matches at least one glob pattern."""
    return any(_glob_match(rel_path, pattern) for pattern in patterns)


def _scan_patterns(
    base_dir: Path, patterns: dict[str, list[str]], collection: PrimitiveCollection, source: str
) -> None:
    """Walk *base_dir* once, match files against all patterns, parse and collect.

    Replaces the previous per-pattern ``glob.glob`` loop with a single
    ``os.walk`` pass, reducing filesystem traversals from O(patterns) to O(1).

    Args:
        base_dir: Directory to scan (e.g., dep/.apm or dep/.github).
        patterns: Primitive-type → glob-pattern mapping.
        collection: Collection to add primitives to.
        source: Source identifier for discovered primitives.
    """
    if not base_dir.exists():
        return

    # Flatten all patterns into a single list for matching
    all_patterns: list[str] = []
    for _primitive_type, type_patterns in patterns.items():
        all_patterns.extend(type_patterns)

    base_str = str(base_dir)
    for dirpath, _dirnames, filenames in os.walk(base_str, followlinks=False):
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(full_path, base_str).replace(os.sep, "/")
            if not _matches_any_pattern(rel_path, all_patterns):
                continue
            file_path = Path(full_path)
            if file_path.is_file() and _is_readable(file_path):
                try:
                    primitive = parse_primitive_file(file_path, source=source)
                    collection.add_primitive(primitive)
                except Exception as e:
                    print(f"Warning: Failed to parse dependency primitive {file_path}: {e}")


def scan_directory_with_source(
    directory: Path, collection: PrimitiveCollection, source: str
) -> None:
    """Scan a directory for primitives with a specific source tag.

    Args:
        directory (Path): Directory to scan (e.g., apm_modules/package_name).
        collection (PrimitiveCollection): Collection to add primitives to.
        source (str): Source identifier for discovered primitives.
    """
    # Scan .apm directory within the dependency
    apm_dir = directory / ".apm"
    if apm_dir.exists():
        _scan_patterns(apm_dir, DEPENDENCY_PRIMITIVE_PATTERNS, collection, source)

    # Also scan .github directory — some packages store primitives there instead of (or
    # in addition to) .apm/.  Without this, dependency instructions in .github/instructions/
    # are silently skipped in the normal compile path (issue #631).
    github_dir = directory / ".github"
    if github_dir.exists():
        _scan_patterns(github_dir, DEPENDENCY_GITHUB_PRIMITIVE_PATTERNS, collection, source)

    # Check for SKILL.md in the dependency root
    _discover_skill_in_directory(directory, collection, source)


def _discover_local_skill(
    base_dir: str,
    collection: PrimitiveCollection,
    exclude_patterns: list[str] | None = None,
) -> None:
    """Discover SKILL.md at the project root.

    Args:
        base_dir (str): Base directory to search in.
        collection (PrimitiveCollection): Collection to add skill to.
        exclude_patterns (Optional[List[str]]): Pre-validated exclude patterns.
    """
    skill_path = Path(base_dir) / "SKILL.md"
    if skill_path.exists() and _is_readable(skill_path):
        if should_exclude(skill_path, Path(base_dir), exclude_patterns):
            logger.debug("Excluded by pattern: %s", skill_path)
            return
        try:
            skill = parse_skill_file(skill_path, source="local")
            collection.add_primitive(skill)
        except Exception as e:
            print(f"Warning: Failed to parse SKILL.md: {e}")


def _discover_skill_in_directory(
    directory: Path, collection: PrimitiveCollection, source: str
) -> None:
    """Discover SKILL.md in a package directory.

    Args:
        directory (Path): Package directory to check.
        collection (PrimitiveCollection): Collection to add skill to.
        source (str): Source identifier for the skill.
    """
    skill_path = directory / "SKILL.md"
    if skill_path.exists() and _is_readable(skill_path):
        try:
            skill = parse_skill_file(skill_path, source=source)
            collection.add_primitive(skill)
        except Exception as e:
            print(f"Warning: Failed to parse SKILL.md in {directory}: {e}")
