"""Dependency reference models and Git reference utilities."""

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
    "MCPDependency",
    "RemoteRef",
    "ResolvedReference",
    "VirtualPackageType",
    "parse_git_reference",
]
