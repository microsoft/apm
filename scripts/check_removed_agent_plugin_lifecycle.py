#!/usr/bin/env python3
"""Reject reintroduction of removed native Agent Plugin lifecycle state."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

REMOVED_PATHS = (
    "src/apm_cli/install/agent_plugin_runtime.py",
    "src/apm_cli/install/agent_plugin_state.py",
)

REMOVED_SYMBOLS = (
    "AgentPluginRootLayout",
    "InstalledPluginComponentFact",
    "InstalledPluginRecord",
    "InstalledPluginRecordCodec",
    "PreparedAgentPluginRoot",
    "PreparedInstalledPluginState",
    "commit_agent_plugin_bundle",
    "discard_staged_agent_plugin_bundle",
    "installed_plugins",
    "materialize_agent_plugin_bundle",
    "prepare_agent_plugin_root",
    "prepare_installed_plugin_state",
    "project_installed_plugin_record",
    "remove_installed_plugin_root",
    "replace_installed_plugins",
    "resolve_agent_plugin_roots",
    "resolve_installed_plugin_record_roots",
    "stable_agent_plugin_id",
    "stage_agent_plugin_bundle",
)

REMOVED_LOCAL_BUNDLE_FIELDS = (
    "data_root",
    "retained_root",
    "source_identity",
)

REMOVED_LOCAL_HANDLER_SYMBOLS = ("runtime_root",)


def _find_tokens(path: Path, tokens: tuple[str, ...]) -> list[str]:
    if not path.is_file():
        return []
    source = path.read_text(encoding="utf-8")
    return [token for token in tokens if re.search(rf"\b{re.escape(token)}\b", source) is not None]


def check(root: Path) -> list[str]:
    violations = [
        f"removed lifecycle module exists: {relative_path}"
        for relative_path in REMOVED_PATHS
        if (root / relative_path).exists()
    ]
    source_root = root / "src/apm_cli"
    for path in sorted(source_root.rglob("*.py")) if source_root.is_dir() else ():
        for token in _find_tokens(path, REMOVED_SYMBOLS):
            violations.append(f"removed lifecycle symbol {token!r} in {path.relative_to(root)}")
    scoped_tokens = (
        ("src/apm_cli/bundle/local_bundle.py", REMOVED_LOCAL_BUNDLE_FIELDS),
        ("src/apm_cli/install/local_bundle_handler.py", REMOVED_LOCAL_HANDLER_SYMBOLS),
    )
    for relative_path, tokens in scoped_tokens:
        for token in _find_tokens(root / relative_path, tokens):
            violations.append(f"removed lifecycle field {token!r} in {relative_path}")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    violations = check(args.root.resolve())
    if violations:
        for violation in violations:
            print(f"[x] {violation}")
        return 1
    print("[+] Removed Agent Plugin lifecycle tombstone intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
