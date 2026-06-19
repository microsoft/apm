"""Shared constants for the APM CLI."""

from enum import Enum

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class InstallMode(Enum):
    """Controls which dependency types are installed."""

    ALL = "all"
    APM = "apm"
    MCP = "mcp"


# ---------------------------------------------------------------------------
# File and directory names
# ---------------------------------------------------------------------------
APM_YML_FILENAME = "apm.yml"
APM_LOCK_FILENAME = "apm.lock"
APM_MODULES_DIR = "apm_modules"
APM_DIR = ".apm"
SKILL_MD_FILENAME = "SKILL.md"
AGENTS_MD_FILENAME = "AGENTS.md"
CLAUDE_MD_FILENAME = "CLAUDE.md"
GITHUB_DIR = ".github"
CLAUDE_DIR = ".claude"
GITIGNORE_FILENAME = ".gitignore"
APM_MODULES_GITIGNORE_PATTERN = "apm_modules/"


# ---------------------------------------------------------------------------
# Directory names unconditionally skipped during primitive-file discovery.
# These never contain APM primitives or user source files and can be very
# large (e.g. node_modules, .git objects). Used by find_primitive_files()
# in primitives/discovery.py to prune traversal.
# NOTE: .apm is intentionally absent -- it is where primitives live.
# ---------------------------------------------------------------------------
DEFAULT_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".venv",
        "venv",
        ".tox",
        "build",
        "dist",
        ".mypy_cache",
        "apm_modules",
        # Common vendored / generated package locations across ecosystems.
        # These never contain user-authored primitives and can be huge
        # (the Kubernetes vendor/ tree alone is ~14k files; CocoaPods'
        # Pods/ tree, bower_components, jspm_packages, and the various
        # staging/third_party trees in Google-style monorepos behave the
        # same way). Pruning at the directory level avoids the per-file
        # cost in find_primitive_files. See issue #1533.
        "vendor",
        "third_party",
        "Pods",
        "bower_components",
        "jspm_packages",
        ".gradle",
        "target",
        ".next",
        ".nuxt",
        ".cache",
        ".turbo",
    }
)
