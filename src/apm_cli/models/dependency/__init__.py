"""Dependency reference models and Git reference utilities."""

from .lsp import LSPDependency
from .mcp import MCPDependency
from .reference import DependencyReference
from .replacement import (
    GitReplacementSource,
    LocalReplacementSource,
    ReplacementCollisionError,
    ReplacementConfigError,
    ReplacementCycleError,
    ReplacementIndex,
    ReplacementRule,
    ReplacementSelector,
    build_replacement_index,
    parse_replace_block,
    project_effective_reference,
    replacement_rule_to_dict,
    replacement_rules_from_dicts,
)
from .types import (
    GitReferenceType,
    RemoteRef,
    ResolvedReference,
    VirtualPackageType,
    parse_git_reference,
)

__all__ = [
    "DependencyReference",
    "GitReferenceType",
    "GitReplacementSource",
    "LSPDependency",
    "LocalReplacementSource",
    "MCPDependency",
    "RemoteRef",
    "ReplacementCollisionError",
    "ReplacementConfigError",
    "ReplacementCycleError",
    "ReplacementIndex",
    "ReplacementRule",
    "ReplacementSelector",
    "ResolvedReference",
    "VirtualPackageType",
    "build_replacement_index",
    "parse_git_reference",
    "parse_replace_block",
    "project_effective_reference",
    "replacement_rule_to_dict",
    "replacement_rules_from_dicts",
]
