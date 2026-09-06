"""Built-in discovery scanners, registered in evaluation order."""

from __future__ import annotations

from ..registry import REGISTRY, ScannerRegistry
from .hooks import HooksScanner
from .mcp import McpScanner
from .plugins import PluginsScanner
from .profile_files import ProfileFilesScanner
from .root_context import RootContextScanner


def register_builtin_scanners(registry: ScannerRegistry = REGISTRY) -> ScannerRegistry:
    """Populate *registry* with the built-in scanners (idempotent)."""
    existing = {scanner.name for scanner in registry.scanners()}
    for scanner in (
        ProfileFilesScanner(),
        RootContextScanner(),
        HooksScanner(),
        McpScanner(),
        PluginsScanner(),
    ):
        if scanner.name not in existing:
            registry.register(scanner)
    return registry


__all__ = [
    "HooksScanner",
    "McpScanner",
    "PluginsScanner",
    "ProfileFilesScanner",
    "RootContextScanner",
    "register_builtin_scanners",
]
