"""Dependency reference models and Git reference utilities."""

from .lsp import LSPDependency
from .mcp import MCPDependency
from .reference import DependencyReference
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
    "LSPDependency",
    "MCPDependency",
    "RemoteRef",
    "ResolvedReference",
    "VirtualPackageType",
    "parse_git_reference",
]
