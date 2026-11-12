"""Canonical project-name validation for manifest bootstrap paths."""

DEFAULT_BOOTSTRAP_PROJECT_NAME = "my-project"


def validate_project_name(name: str) -> bool:
    """Return whether a project name is safe to use as a directory name."""
    if not name or not name.strip():
        return False
    if "/" in name or "\\" in name:
        return False
    return name != ".."


def resolve_bootstrap_project_name(candidate: str) -> str:
    """Return a valid name for a manifest created by an automatic path."""
    if validate_project_name(candidate):
        return candidate
    return DEFAULT_BOOTSTRAP_PROJECT_NAME
