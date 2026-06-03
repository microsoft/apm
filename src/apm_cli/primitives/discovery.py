"""Discovery functionality for primitive files."""

import fnmatch
import logging
import os
import time
from pathlib import Path

from ..constants import DEFAULT_SKIP_DIRS
from ..utils import perf_stats
from ..utils.exclude import should_exclude, validate_exclude_patterns
from .models import PrimitiveCollection
from .parser import parse_primitive_file, parse_skill_file

logger = logging.getLogger(__name__)
from ..deps.lockfile import LockFile  # noqa: E402
from ..models.apm_package import APMPackage  # noqa: E402

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

# Canonical primitive-file extensions, derived from LOCAL_PRIMITIVE_PATTERNS
# so a new primitive type added there is automatically recognized anywhere
# the suffix set is consumed (e.g. ``apm compile --watch`` smart-skip).
# Computed at module load (one allocation) and exposed as a frozenset for
# O(1) membership testing on the hot path. SKILL files are matched by
# exact basename, not suffix, so they are NOT included here -- callers that
# care about skills must additionally check ``os.path.basename(p) == "SKILL.md"``.
PRIMITIVE_SUFFIXES: frozenset[str] = frozenset(
    "." + pattern.rsplit("*.", 1)[1]
    for patterns in LOCAL_PRIMITIVE_PATTERNS.values()
    for pattern in patterns
    if "*." in pattern
)


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


# Process-scoped memo for ``discover_primitives``. Keyed on the
# resolved absolute base directory + canonical exclude-patterns tuple.
# Cleared explicitly at the start of every install pipeline run by
# ``clear_discovery_cache()``. NOT thread-safe by design -- the
# integrate phase that consumes it is sequential. See issue #1533.
_DISCOVERY_CACHE: dict[tuple[str, tuple[str, ...]], PrimitiveCollection] = {}


def clear_discovery_cache() -> None:
    """Drop all memoized ``discover_primitives`` results.

    Call at the start of every install pipeline invocation so counts
    from earlier runs (tests, REPL, long-lived processes) cannot leak
    into the next install's discovery results.
    """
    _DISCOVERY_CACHE.clear()


def _discovery_cache_key(
    base_dir: str, exclude_patterns: list[str] | None
) -> tuple[str, tuple[str, ...]]:
    """Build a stable cache key for ``discover_primitives``.

    Uses ``os.path.realpath`` instead of ``Path.resolve()`` for symlink
    canonicalization without the per-component pathlib overhead.
    """
    canonical_base = os.path.realpath(base_dir)
    canonical_excl = tuple(sorted(exclude_patterns)) if exclude_patterns else ()
    return (canonical_base, canonical_excl)


def discover_primitives(
    base_dir: str = ".",
    exclude_patterns: list[str] | None = None,
) -> PrimitiveCollection:
    """Find all APM primitive files in the project.

    Searches for .chatmode.md, .instructions.md, .context.md, .memory.md files
    in both .apm/ and .github/ directory structures, plus SKILL.md at root.

    Results are memoized per ``(realpath(base_dir), exclude_patterns)`` for
    the lifetime of the current install pipeline run so that the integrate
    phase's per-(integrator, target) loop does not re-walk the same tree
    N times. See ``clear_discovery_cache()`` for invalidation.

    Args:
        base_dir (str): Base directory to search in. Defaults to current directory.
        exclude_patterns (Optional[List[str]]): Glob patterns for paths to exclude.

    Returns:
        PrimitiveCollection: Collection of discovered and parsed primitives.
    """
    started = time.perf_counter()
    cache_key = _discovery_cache_key(base_dir, exclude_patterns)
    cached = _DISCOVERY_CACHE.get(cache_key)
    if cached is not None:
        perf_stats.record_discovery(
            base_dir=str(base_dir),
            duration_s=time.perf_counter() - started,
            cache_hit=True,
        )
        return cached

    collection = PrimitiveCollection()
    safe_patterns = validate_exclude_patterns(exclude_patterns)

    # Find and parse files for each primitive type
    for primitive_type, patterns in LOCAL_PRIMITIVE_PATTERNS.items():  # noqa: B007
        files = find_primitive_files(base_dir, patterns, exclude_patterns=safe_patterns)

        for file_path in files:
            try:
                primitive = parse_primitive_file(file_path, source="local")
                collection.add_primitive(primitive)
            except Exception as e:
                print(f"Warning: Failed to parse {file_path}: {e}")

    # Discover SKILL.md at project root
    _discover_local_skill(base_dir, collection, exclude_patterns=safe_patterns)

    _DISCOVERY_CACHE[cache_key] = collection
    perf_stats.record_discovery(
        base_dir=str(base_dir),
        duration_s=time.perf_counter() - started,
        cache_hit=False,
    )
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
    for primitive_type, patterns in LOCAL_PRIMITIVE_PATTERNS.items():  # noqa: B007
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


def get_dependency_declaration_order(base_dir: str) -> list[str]:
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

        # Include transitive dependencies + local-bundle slugs from the
        # lockfile.  Read it once and reuse the parsed object for both
        # the transitive-paths walk and the ``local_deployed_files``
        # slug derivation (issue #1363) to avoid duplicate YAML parses
        # on every compile.
        project_root = Path(base_dir)
        lockfile_path = project_root / "apm.lock.yaml"
        if not lockfile_path.exists():
            legacy = project_root / "apm.lock"
            if legacy.exists():
                lockfile_path = legacy
        lock = LockFile.read(lockfile_path) if lockfile_path.exists() else None

        direct_set = set(dependency_names)
        if lock is not None:
            for path in lock.get_installed_paths(project_root / "apm_modules"):
                if path not in direct_set:
                    dependency_names.append(path)

        # Local-bundle install stages instructions under
        # ``apm_modules/<slug>/.apm/...`` but intentionally does NOT
        # mutate ``apm.yml`` (services.py:489-490), so the scan loop
        # would otherwise never visit those staged dirs and
        # ``apm compile`` would produce no output for compile-only
        # targets (opencode, codex, gemini).
        #
        # Provenance is anchored to the lockfile record -- a stray
        # directory under ``apm_modules/`` without a lockfile entry must
        # not be discovered (defends against phantom-content injection
        # and stale-debris drift).
        if lock is not None:
            local_slugs: set[str] = set()
            for deployed in lock.local_deployed_files:
                # Match ``apm_modules/<slug>/.apm/...`` only. Other
                # deployed files (``.github/instructions/...``,
                # ``.agents/skills/...``) are not bundle staging
                # markers and must not produce phantom slugs.
                parts = Path(deployed).parts
                if len(parts) >= 3 and parts[0] == "apm_modules" and parts[2] == ".apm":
                    local_slugs.add(parts[1])
            seen = set(dependency_names)
            for slug in sorted(local_slugs):
                if slug not in seen:
                    dependency_names.append(slug)
                    seen.add(slug)

        return dependency_names

    except Exception as e:
        print(f"Warning: Failed to parse dependency order from apm.yml: {e}")
        return []


def _matches_any_pattern(rel_path: str, patterns: list[str]) -> bool:
    """Return ``True`` if *rel_path* matches at least one glob pattern."""
    for pattern in patterns:  # noqa: SIM110
        if _glob_match(rel_path, pattern):
            return True
    return False


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
    path_parts: list[str] = [p for p in rel_path.split("/") if p]
    pattern_parts: list[str] = [p for p in pattern.split("/") if p]
    return _glob_match_parts(tuple(path_parts), tuple(pattern_parts))


def _glob_match_parts(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
    """Variant of :func:`_glob_match` that accepts pre-split tuples.

    Hot-path optimization: ``find_primitive_files`` pre-splits patterns
    once per call (instead of once per file) and re-uses the segment
    tuple. The split path tuple changes per file but is cheap.
    """
    memo: dict[tuple[int, int], bool] = {}

    def _match(pi: int, qi: int) -> bool:
        key = (pi, qi)
        if key in memo:
            return memo[key]

        if qi == len(pattern_parts):
            result = pi == len(path_parts)
            memo[key] = result
            return result

        current = pattern_parts[qi]

        if current == "**":
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
        result = fnmatch.fnmatch(path_parts[pi], current) and _match(pi + 1, qi + 1)
        memo[key] = result
        return result

    return _match(0, 0)


def find_primitive_files(
    base_dir: str,
    patterns: list[str],
    exclude_patterns: list[str] | None = None,
) -> list[Path]:
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

    started = time.perf_counter()
    base_path = Path(base_dir).resolve()
    base_str = str(base_path)
    base_prefix_len = len(base_str) + 1  # +1 for the trailing separator
    sep = os.sep

    # Pre-split each glob pattern once per call instead of once per file
    # so a 80k-file walk costs O(patterns) splits, not O(patterns * files).
    pattern_tuples: list[tuple[str, ...]] = [
        tuple(p for p in pat.split("/") if p) for pat in patterns
    ]

    all_files: list[Path] = []
    files_visited = 0

    for root, dirs, files in os.walk(base_str):
        # Prune excluded directories BEFORE descending. ``DEFAULT_SKIP_DIRS``
        # check is a frozenset lookup; the ``_exclude_matches_dir`` call
        # only fires when the caller actually supplied exclude patterns.
        if exclude_patterns:
            current = Path(root)
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in DEFAULT_SKIP_DIRS
                and not _exclude_matches_dir(current / d, base_path, exclude_patterns)
            )
        else:
            dirs[:] = sorted(d for d in dirs if d not in DEFAULT_SKIP_DIRS)

        # Compute the relative directory once per ``os.walk`` step using
        # string slicing on the already-resolved base path. This avoids
        # the per-component ``stat`` syscalls that ``Path.resolve`` /
        # ``Path.relative_to`` would issue per FILE under the old
        # ``portable_relpath(file_path, base_path)`` call site.
        if root == base_str:
            rel_root = ""
            rel_root_parts: tuple[str, ...] = ()
        else:
            rel_root = root[base_prefix_len:].replace(sep, "/")
            rel_root_parts = tuple(p for p in rel_root.split("/") if p)

        # Sort files for deterministic discovery order across platforms.
        # Defer all Path() construction until AFTER a pattern matches --
        # in a typical tree most files are non-matches and don't need
        # the allocation. ``current`` is built lazily on first match.
        sorted_files = sorted(files)
        files_visited += len(sorted_files)
        current_path: Path | None = None
        for file_name in sorted_files:
            path_parts = (*rel_root_parts, file_name)
            matched_pattern = False
            for pattern_parts in pattern_tuples:
                if _glob_match_parts(path_parts, pattern_parts):
                    matched_pattern = True
                    break
            if not matched_pattern:
                continue
            if current_path is None:
                current_path = Path(root)
            file_path = current_path / file_name
            # File-level exclude: a pattern like "**/*.draft.md" should drop
            # individual files even when their parent directory is included.
            if exclude_patterns and should_exclude(file_path, base_path, exclude_patterns):
                logger.debug("Excluded by pattern: %s", file_path)
                continue
            all_files.append(file_path)

    # Filter out directories and symlinks. We deliberately do NOT
    # pre-open every match to test readability -- ``parse_primitive_file``
    # downstream already handles PermissionError / UnicodeDecodeError
    # gracefully, and the extra open() per match doubled syscall cost
    # without catching anything new (see #1533 review).
    valid_files = []
    for file_path in all_files:
        if not file_path.is_file():
            continue
        if file_path.is_symlink():
            logger.debug("Rejected symlink: %s", file_path)
            continue
        valid_files.append(file_path)

    perf_stats.record_walk(
        base_dir=str(base_dir),
        pattern_count=len(patterns),
        duration_s=time.perf_counter() - started,
        files_visited=files_visited,
        files_matched=len(valid_files),
    )
    return valid_files


def _exclude_matches_dir(
    dir_path: Path,
    base_path: Path,
    exclude_patterns: list[str] | None,
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
        with open(file_path, encoding="utf-8") as f:
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
