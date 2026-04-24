"""Discovery functionality for primitive files."""

import fnmatch
import glob
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import PrimitiveCollection
from .parser import parse_primitive_file, parse_skill_file
from ..constants import DEFAULT_SKIP_DIRS
from ..utils.exclude import should_exclude, validate_exclude_patterns
from ..utils.paths import portable_relpath

logger = logging.getLogger(__name__)
from ..models.apm_package import APMPackage
from ..deps.lockfile import LockFile


# Common primitive patterns for local discovery (with recursive search)
LOCAL_PRIMITIVE_PATTERNS: Dict[str, List[str]] = {
    'chatmode': [
        # New standard (.agent.md)
        "**/.apm/agents/*.agent.md",
        "**/.github/agents/*.agent.md",
        "**/*.agent.md",  # Generic .agent.md files
        # Legacy support (.chatmode.md)
        "**/.apm/chatmodes/*.chatmode.md",
        "**/.github/chatmodes/*.chatmode.md",
        "**/*.chatmode.md"  # Generic .chatmode.md files
    ],
    'instruction': [
        "**/.apm/instructions/*.instructions.md",
        "**/.github/instructions/*.instructions.md",
        "**/*.instructions.md"  # Generic .instructions.md files
    ],
    'context': [
        "**/.apm/context/*.context.md",
        "**/.apm/memory/*.memory.md",  # APM memory convention
        "**/.github/context/*.context.md",
        "**/.github/memory/*.memory.md",  # VSCode compatibility
        "**/*.context.md",  # Generic .context.md files
        "**/*.memory.md"  # Generic .memory.md files
    ]
}

# Dependency primitive patterns (for .apm directory within dependencies)
DEPENDENCY_PRIMITIVE_PATTERNS: Dict[str, List[str]] = {
    'chatmode': [
        "agents/*.agent.md",  # New standard
        "chatmodes/*.chatmode.md"  # Legacy
    ],
    'instruction': ["instructions/*.instructions.md"],
    'context': [
        "context/*.context.md",
        "memory/*.memory.md"
    ]
}

# Dependency primitive patterns for .github directory within dependencies.
# Some packages store primitives in .github/ instead of (or in addition to) .apm/.
DEPENDENCY_GITHUB_PRIMITIVE_PATTERNS: Dict[str, List[str]] = {
    'chatmode': [
        "agents/*.agent.md",
        "chatmodes/*.chatmode.md",
    ],
    'instruction': ["instructions/*.instructions.md"],
    'context': [
        "context/*.context.md",
        "memory/*.memory.md",
    ]
}


def discover_primitives(
    base_dir: str = ".",
    exclude_patterns: Optional[List[str]] = None,
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
    base_path = Path(base_dir)
    safe_patterns = validate_exclude_patterns(exclude_patterns)
    
    # Find and parse files for each primitive type
    for primitive_type, patterns in LOCAL_PRIMITIVE_PATTERNS.items():
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
    exclude_patterns: Optional[List[str]] = None,
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
    exclude_patterns: Optional[List[str]] = None,
) -> None:
    """Scan local .apm/ directory for primitives.
    
    Args:
        base_dir (str): Base directory to search in.
        collection (PrimitiveCollection): Collection to add primitives to.
        exclude_patterns (Optional[List[str]]): Pre-validated exclude patterns.
    """
    # Find and parse files for each primitive type
    for primitive_type, patterns in LOCAL_PRIMITIVE_PATTERNS.items():
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


def get_dependency_declaration_order(base_dir: str) -> List[str]:
    """Get APM dependency installed paths in their declaration order.
    
    The returned list contains the actual installed path for each dependency,
    combining:
    1. Direct dependencies from apm.yml (highest priority, declaration order)
    2. Transitive dependencies from apm.lock (appended after direct deps)
    
    This ensures transitive dependencies are included in primitive discovery
    and compilation, not just direct dependencies. The installed path differs for:
    - Regular packages: owner/repo (GitHub) or org/project/repo (ADO)
    - Virtual packages: owner/virtual-pkg-name (GitHub) or org/project/virtual-pkg-name (ADO)
    
    Args:
        base_dir (str): Base directory containing apm.yml.
    
    Returns:
        List[str]: List of dependency installed paths in declaration order.
    """
    try:
        apm_yml_path = Path(base_dir) / "apm.yml"
        if not apm_yml_path.exists():
            return []
        
        package = APMPackage.from_apm_yml(apm_yml_path)
        apm_dependencies = package.get_apm_dependencies()
        
        # Extract installed paths from dependency references
        # Virtual file/collection packages use get_virtual_package_name() (flattened),
        # while virtual subdirectory packages use natural repo/subdir paths.
        dependency_names = []
        for dep in apm_dependencies:
            if dep.alias:
                dependency_names.append(dep.alias)
            elif dep.is_virtual:
                repo_parts = dep.repo_url.split("/")

                if dep.is_virtual_subdirectory() and dep.virtual_path:
                    # Virtual subdirectory packages keep natural path structure.
                    # GitHub: owner/repo/subdir
                    # ADO: org/project/repo/subdir
                    if dep.is_azure_devops() and len(repo_parts) >= 3:
                        dependency_names.append(
                            f"{repo_parts[0]}/{repo_parts[1]}/{repo_parts[2]}/{dep.virtual_path}"
                        )
                    elif len(repo_parts) >= 2:
                        dependency_names.append(
                            f"{repo_parts[0]}/{repo_parts[1]}/{dep.virtual_path}"
                        )
                    else:
                        dependency_names.append(dep.virtual_path)
                else:
                    # Virtual file/collection packages are flattened by package name.
                    # GitHub: owner/virtual-pkg-name
                    # ADO: org/project/virtual-pkg-name
                    virtual_name = dep.get_virtual_package_name()
                    if dep.is_azure_devops() and len(repo_parts) >= 3:
                        dependency_names.append(f"{repo_parts[0]}/{repo_parts[1]}/{virtual_name}")
                    elif len(repo_parts) >= 2:
                        dependency_names.append(f"{repo_parts[0]}/{virtual_name}")
                    else:
                        dependency_names.append(virtual_name)
            else:
                # Regular packages: use full org/repo path
                # This matches our org-namespaced directory structure
                dependency_names.append(dep.repo_url)
        
        # Include transitive dependencies from apm.lock
        # Direct deps from apm.yml have priority; transitive deps are appended
        lockfile_paths = LockFile.installed_paths_for_project(Path(base_dir))
        direct_set = set(dependency_names)
        for path in lockfile_paths:
            if path not in direct_set:
                dependency_names.append(path)
        
        return dependency_names
        
    except Exception as e:
        print(f"Warning: Failed to parse dependency order from apm.yml: {e}")
        return []


def _scan_patterns(base_dir: Path, patterns: Dict[str, List[str]], collection: PrimitiveCollection, source: str) -> None:
    """Glob-scan-parse loop for one base directory and one patterns dict.

    Args:
        base_dir (Path): Directory to scan (e.g., dep/.apm or dep/.github).
        patterns (Dict[str, List[str]]): Primitive-type → glob-pattern mapping.
        collection (PrimitiveCollection): Collection to add primitives to.
        source (str): Source identifier for discovered primitives.
    """
    for _primitive_type, type_patterns in patterns.items():
        for pattern in type_patterns:
            for file_path_str in glob.glob(str(base_dir / pattern), recursive=True):
                file_path = Path(file_path_str)
                if file_path.is_file() and _is_readable(file_path):
                    try:
                        primitive = parse_primitive_file(file_path, source=source)
                        collection.add_primitive(primitive)
                    except Exception as e:
                        print(f"Warning: Failed to parse dependency primitive {file_path}: {e}")


def scan_directory_with_source(directory: Path, collection: PrimitiveCollection, source: str) -> None:
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
    exclude_patterns: Optional[List[str]] = None,
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


def _discover_skill_in_directory(directory: Path, collection: PrimitiveCollection, source: str) -> None:
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


def _glob_match(rel_path: str, pattern: str) -> bool:
    """Match a forward-slash relative path against a glob pattern.

    Segment-aware: ``*`` and ``?`` match within a single path segment only,
    while ``**`` matches zero or more complete segments. This preserves
    standard glob semantics so a pattern like
    ``**/.apm/instructions/*.instructions.md`` does not accidentally match
    ``.apm/instructions/sub/x.instructions.md`` (the trailing ``*`` must
    not cross ``/``).

    Args:
        rel_path: Relative path using forward slashes.
        pattern: Glob pattern using forward slashes.

    Returns:
        True if the path matches the pattern.
    """
    path_parts: List[str] = [p for p in rel_path.split('/') if p]
    pattern_parts: List[str] = [p for p in pattern.split('/') if p]
    memo: Dict[Tuple[int, int], bool] = {}

    def _match(pi: int, qi: int) -> bool:
        key = (pi, qi)
        if key in memo:
            return memo[key]

        if qi == len(pattern_parts):
            result = pi == len(path_parts)
            memo[key] = result
            return result

        current = pattern_parts[qi]

        if current == '**':
            # ** matches zero segments, OR consumes one segment and stays at **
            result = _match(pi, qi + 1)
            if not result and pi < len(path_parts):
                result = _match(pi + 1, qi)
            memo[key] = result
            return result

        if pi >= len(path_parts):
            memo[key] = False
            return False

        # Use platform-aware fnmatch semantics so Windows matching remains
        # case-insensitive, consistent with prior glob.glob() behavior.
        result = (
            fnmatch.fnmatch(path_parts[pi], current)
            and _match(pi + 1, qi + 1)
        )
        memo[key] = result
        return result

    return _match(0, 0)


def find_primitive_files(
    base_dir: str,
    patterns: List[str],
    exclude_patterns: Optional[List[str]] = None,
) -> List[Path]:
    """Find primitive files matching the given patterns.

    Uses os.walk with early directory pruning instead of glob.glob(recursive=True)
    so that exclude_patterns prevent traversal into expensive subtrees.

    Symlinks are rejected outright to prevent symlink-based traversal
    attacks from malicious packages.

    Args:
        base_dir (str): Base directory to search in.
        patterns (List[str]): List of glob patterns to match.
        exclude_patterns (Optional[List[str]]): Pre-validated exclude patterns
            to prune directories early during traversal.

    Returns:
        List[Path]: List of file paths found.
    """
    if not os.path.isdir(base_dir):
        return []

    base_path = Path(base_dir).resolve()

    all_files: List[Path] = []

    for root, dirs, files in os.walk(str(base_path)):
        current = Path(root)
        # Prune excluded directories BEFORE descending
        dirs[:] = sorted(
            d for d in dirs
            if d not in DEFAULT_SKIP_DIRS
            and not _exclude_matches_dir(current / d, base_path, exclude_patterns)
        )

        # Sort files for deterministic discovery order across platforms
        for file_name in sorted(files):
            file_path = current / file_name
            rel_str = portable_relpath(file_path, base_path)
            # File-level exclude: a pattern like "**/*.draft.md" should drop
            # individual files even when their parent directory is included.
            if exclude_patterns and should_exclude(file_path, base_path, exclude_patterns):
                logger.debug("Excluded by pattern: %s", file_path)
                continue
            for pattern in patterns:
                if _glob_match(rel_str, pattern):
                    all_files.append(file_path)
                    break

    # Filter out directories, symlinks, and unreadable files
    valid_files = []
    for file_path in all_files:
        if not file_path.is_file():
            continue
        if file_path.is_symlink():
            logger.debug("Rejected symlink: %s", file_path)
            continue
        if _is_readable(file_path):
            valid_files.append(file_path)

    return valid_files


def _exclude_matches_dir(
    dir_path: Path,
    base_path: Path,
    exclude_patterns: Optional[List[str]],
) -> bool:
    """Check if a directory matches any exclude pattern (for early pruning)."""
    if not exclude_patterns:
        return False
    return should_exclude(dir_path, base_path, exclude_patterns)


def _is_readable(file_path: Path) -> bool:
    """Check if a file is readable.
    
    Args:
        file_path (Path): Path to check.
    
    Returns:
        bool: True if file is readable, False otherwise.
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            # Try to read first few bytes to verify it's readable
            f.read(1)
        return True
    except (PermissionError, UnicodeDecodeError, OSError):
        return False


def _should_skip_directory(dir_path: str) -> bool:
    """Check if a directory should be skipped during scanning.
    
    Args:
        dir_path (str): Directory path to check.
    
    Returns:
        bool: True if directory should be skipped, False otherwise.
    """
    dir_name = os.path.basename(dir_path)
    return dir_name in DEFAULT_SKIP_DIRS