"""Scanner for plugin-shaped layouts that APM installs rather than converts."""

from __future__ import annotations

from collections.abc import Iterable

from apm_cli.bundle.plugin_layout import find_plugin_root_sources

from ..model import HarnessKind, RawFinding, Scope
from ..registry import ScanContext


class PluginsScanner:
    """Report plugin-native root layouts and Claude plugin manifests."""

    name = "plugins"

    def scan(self, ctx: ScanContext) -> Iterable[RawFinding]:
        if ctx.scope is not Scope.PROJECT:
            return
        manifest = ctx.root / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            yield RawFinding(
                tool="claude",
                scope=ctx.scope,
                kind=HarnessKind.PLUGIN,
                display_path=ctx.display(manifest),
                abs_path=manifest,
                size_bytes=manifest.stat().st_size,
                format_id="claude_plugin",
                notes=("project is a Claude plugin; install it with `apm install ./`",),
                evidence=("plugin:manifest",),
            )
        for source in find_plugin_root_sources(ctx.root):
            path = ctx.root / source
            yield RawFinding(
                tool="root",
                scope=ctx.scope,
                kind=HarnessKind.PLUGIN,
                display_path=ctx.display(path),
                abs_path=path,
                size_bytes=path.stat().st_size if path.is_file() else None,
                format_id="plugin_root_layout",
                notes=("plugin-native root layout; `apm pack` already includes it",),
                evidence=("plugin:root-source",),
            )
