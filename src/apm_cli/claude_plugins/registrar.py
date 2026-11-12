"""Single reconciliation owner for APM's native Claude user marketplace.

Claude owns its settings, cache and installed-plugin registry. APM writes
only a directory catalog and a recoverable intent ledger in apm_modules.
Native operations are not a byte-rollback transaction: a retry checks live
state against intent before continuing a partially completed reconciliation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from apm_cli.core.scope import is_user_scope
from apm_cli.utils.atomic_io import atomic_write_text
from apm_cli.utils.path_security import ensure_path_within, has_symlink_component
from apm_cli.utils.paths import portable_relpath

from .admission import (
    ClaudeNativePlugin,
    ClaudeNativePluginError,
    fingerprint_native_claude_source,
)
from .client import ClaudePluginCLI

MARKETPLACE_NAME = "apm-claude"
CATALOG_RELATIVE = ".claude-plugin/marketplace.json"
LEDGER_RELATIVE = ".claude-plugin/apm-registration.json"


def _read_ledger(path: Path, modules_dir: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        entries = document["plugins"]
        version = document["version"]
        if (
            type(version) is not int
            or version not in {1, 2}
            or document["marketplace"] != MARKETPLACE_NAME
        ):
            raise ValueError("unsupported ledger")
        if not isinstance(entries, dict):
            raise ValueError("invalid plugin map")
        for plugin_id, entry in entries.items():
            # Intent can embed retiring v1 entries in a v2 ledger. Retaining
            # those identities permits removal retries, never fresh attestation.
            if (
                not isinstance(plugin_id, str)
                or not plugin_id.endswith("@" + MARKETPLACE_NAME)
                or not isinstance(entry, dict)
                or set(entry)
                not in (
                    {"dependency_key", "source"},
                    {"dependency_key", "source", "bundle_hash", "source_pin"}
                    if version == 2
                    else set(),
                )
                or not all(isinstance(value, str) and value for value in entry.values())
            ):
                raise ValueError("invalid plugin ownership")
            ensure_path_within(modules_dir / entry["source"], modules_dir)
            if (
                "bundle_hash" in entry
                and re.fullmatch(r"sha256:[0-9a-f]{64}", entry["bundle_hash"]) is None
            ):
                raise ValueError("invalid native fingerprint")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ClaudeNativePluginError(
            "Cannot safely read APM's Claude ownership ledger; refusing native state changes."
        ) from exc
    return entries


def _write_ledger(path: Path, entries: dict[str, dict[str, str]]) -> None:
    atomic_write_text(
        path,
        json.dumps(
            {
                "version": 2,
                "marketplace": MARKETPLACE_NAME,
                "plugins": entries,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def _plugin_rows(client: ClaudePluginCLI) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in client.records("plugin", "list", "--json"):
        plugin_id = row.get("id")
        if not isinstance(plugin_id, str):
            raise ClaudeNativePluginError("Claude plugin metadata has no plugin identity.")
        if not plugin_id.endswith("@" + MARKETPLACE_NAME):
            continue
        if row.get("scope") != "user" or plugin_id in result:
            raise ClaudeNativePluginError("APM Claude plugin identity collides with another scope.")
        if not isinstance(row.get("enabled"), bool):
            raise ClaudeNativePluginError("Claude plugin metadata has no explicit enabled state.")
        result[plugin_id] = row
    return result


def _check_live_root(
    row: dict[str, Any],
    entry: dict[str, str],
    modules_dir: Path,
) -> None:
    root = row.get("readFromFolder")
    if (
        not isinstance(root, str)
        or Path(root).resolve() != (modules_dir / entry["source"]).resolve()
    ):
        raise ClaudeNativePluginError(
            "Claude native plugin source ownership changed; refusing to adopt or overwrite it."
        )


def _check_bundle_fingerprint(root: Path, expected: str | None) -> None:
    """Fail closed when an owned or prepared native snapshot cannot be attested."""
    if not expected:
        raise ClaudeNativePluginError(
            "APM's Claude ownership ledger has no complete native fingerprint. "
            "Run 'apm uninstall --global <package>', then reinstall with the original targets."
        )
    if fingerprint_native_claude_source(root) != expected:
        raise ClaudeNativePluginError(
            f"Native Claude source fingerprint changed at {root}. Restore the pinned package "
            "before retrying; APM will not adopt modified publisher resources or executable modes."
        )


def _locked_source_pin(locked: Any) -> str:
    """Bind native ownership to the resolver's saved source identity."""
    revision = (
        locked.local_path
        if locked.source == "local"
        else locked.resolved_commit or locked.resolved_hash
    )
    if not revision:
        raise ClaudeNativePluginError(
            f"Native Claude source {locked.get_unique_key()} has no immutable pin. "
            "Reinstall from a pinned commit or registry artifact."
        )
    return f"{locked.source}:{revision}"


def verify_claude_native_cache(*, modules_dir: Path, locked: Any, source_root: Path) -> None:
    """Verify Claude's saved native baseline without invoking its metadata client."""
    from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError

    try:
        ledger = modules_dir / LEDGER_RELATIVE
        if has_symlink_component(modules_dir, ledger):
            raise ClaudeNativePluginError("Refusing symlinked native Claude ownership ledger.")
        entries = [
            entry
            for entry in _read_ledger(ledger, modules_dir).values()
            if entry["dependency_key"] == locked.get_unique_key()
        ]
        if len(entries) != 1:
            raise ClaudeNativePluginError(
                "Native Claude cache has no unique owned fingerprint. "
                "Reinstall the package with the original targets before auditing."
            )
        entry = entries[0]
        live_root = locked.to_dependency_ref().get_install_path(modules_dir)
        if (modules_dir / entry["source"]).resolve() != live_root.resolve() or entry.get(
            "source_pin"
        ) != _locked_source_pin(locked):
            raise ClaudeNativePluginError(
                "Native Claude fingerprint ownership or source pin changed. "
                "Reinstall with the original target selection before auditing."
            )
        _check_bundle_fingerprint(source_root, entry.get("bundle_hash"))
        if live_root.exists() and live_root.resolve() != source_root.resolve():
            _check_bundle_fingerprint(live_root, entry.get("bundle_hash"))
    except ClaudeNativePluginError as exc:
        raise AgentPluginDeploymentBoundaryError(str(exc)) from exc


def synchronize_claude_plugins(
    *,
    modules_dir: Path,
    scope: Any,
    plugins: Sequence[ClaudeNativePlugin],
    logger: Any = None,
    dry_run: bool = False,
) -> None:
    """Reconcile only owned metadata, preserving native consent and user state."""
    if not is_user_scope(scope) or dry_run:
        return
    modules_dir = Path(modules_dir).absolute()
    catalog = modules_dir / CATALOG_RELATIVE
    ledger = modules_dir / LEDGER_RELATIVE
    if not plugins and not ledger.exists():
        return
    for path in (catalog, ledger):
        ensure_path_within(path, modules_dir)
        if has_symlink_component(modules_dir, path):
            raise ClaudeNativePluginError("Refusing symlinked native Claude registration files.")
    previous = _read_ledger(ledger, modules_dir)
    if catalog.exists() and not ledger.exists():
        raise ClaudeNativePluginError("Claude marketplace catalog is not owned by APM.")
    if not plugins and not previous:
        # Nothing owned or desired: never require the Claude client.
        return

    desired: dict[str, dict[str, str]] = {}
    for plugin in plugins:
        ensure_path_within(plugin.source_root, modules_dir)
        plugin_id = f"{plugin.name}@{MARKETPLACE_NAME}"
        entry = {
            "dependency_key": plugin.dependency_key,
            "source": portable_relpath(plugin.source_root, modules_dir),
            "bundle_hash": plugin.bundle_hash,
            "source_pin": plugin.source_pin,
        }
        if plugin_id in desired or (
            plugin_id in previous
            and any(previous[plugin_id][key] != entry[key] for key in ("dependency_key", "source"))
        ):
            raise ClaudeNativePluginError(
                f"Native Claude plugin name collision: {plugin.name}. Use distinct plugin names."
            )
        if not plugin.source_pin:
            raise ClaudeNativePluginError(
                "Native Claude admission has no immutable source identity. "
                "Reinstall the package from a pinned source."
            )
        # An admitted plan is not exclusive filesystem ownership: another
        # process can change its source before this independently callable step.
        _check_bundle_fingerprint(plugin.source_root, plugin.bundle_hash)
        desired[plugin_id] = entry

    client = ClaudePluginCLI()
    client.check_version()
    marketplaces = client.records("plugin", "marketplace", "list", "--json")
    registered = [row for row in marketplaces if row.get("name") == MARKETPLACE_NAME]
    if len(registered) > 1 or (
        registered
        and (
            not ledger.exists()
            or registered[0].get("source") != "directory"
            or not isinstance(registered[0].get("path"), str)
            or Path(registered[0]["path"]).resolve() != modules_dir.resolve()
        )
    ):
        raise ClaudeNativePluginError(
            "The apm-claude marketplace is already owned by another source. "
            "Resolve the collision in Claude before installing."
        )
    live = _plugin_rows(client)
    for plugin_id, row in live.items():
        if plugin_id not in previous:
            raise ClaudeNativePluginError(
                "The apm-claude marketplace contains an unowned plugin; refusing to mutate it."
            )
        _check_live_root(row, previous[plugin_id], modules_dir)

    catalog.parent.mkdir(parents=True, exist_ok=True)
    # Intent precedes foreign writes. Preserve retiring identities until the
    # native CLI confirms removal, including when a previous attempt crashed.
    _write_ledger(ledger, previous | desired)
    removed = set(previous) - set(desired)
    for plugin_id in sorted(removed):
        if plugin_id in live:
            client.run("plugin", "uninstall", plugin_id, "--scope", "user", "--keep-data")
    atomic_write_text(
        catalog,
        json.dumps(
            {
                "name": MARKETPLACE_NAME,
                "owner": {"name": "APM"},
                "plugins": [
                    {
                        "name": plugin_id.split("@", 1)[0],
                        "source": "./" + entry["source"],
                        "defaultEnabled": False,
                    }
                    for plugin_id, entry in sorted(desired.items())
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    if desired and not registered:
        client.run("plugin", "marketplace", "add", str(modules_dir), "--scope", "user")
    for plugin_id in sorted(desired):
        operation = "update" if plugin_id in live else "install"
        client.run("plugin", operation, plugin_id, "--scope", "user")
    after = _plugin_rows(client)
    if set(after) != set(desired):
        raise ClaudeNativePluginError(
            "Claude did not converge to the requested plugin registration. "
            "APM retained its intent ledger; retry with the original target selection."
        )
    for plugin_id, entry in desired.items():
        _check_live_root(after[plugin_id], entry, modules_dir)
        _check_bundle_fingerprint(modules_dir / entry["source"], entry["bundle_hash"])
        expected_enabled = live[plugin_id]["enabled"] if plugin_id in live else False
        if after[plugin_id]["enabled"] != expected_enabled:
            raise ClaudeNativePluginError(
                "Claude changed a plugin's native activation state unexpectedly. "
                "Review it in Claude; APM will not override native user consent."
            )
    _write_ledger(ledger, desired)
    if logger and removed:
        logger.tree_item(
            f"{len(removed)} native Claude plugin(s) removed; native plugin data retained."
        )
    if logger and desired:
        logger.tree_item(
            f"{len(desired)} native Claude plugin(s) registered; existing activation preserved. "
            "New plugins are disabled. Review and enable them in Claude; restart or "
            "reload plugins there. Hook execution has not been verified by this install."
        )


def _verify_locked_source_hash(
    locked: Any, lockfile: Any, modules_dir: Path, root: Path, key: str
) -> None:
    """Compare the lock against its source image, not local normalization output."""
    from apm_cli.deps.path_anchoring import LocalResolutionError, resolve_local_dep_dir
    from apm_cli.install.phases.local_content import compute_local_package_hash
    from apm_cli.utils.content_hash import compute_package_hash

    if not locked.content_hash:
        return
    try:
        observed = (
            compute_local_package_hash(resolve_local_dep_dir(locked, lockfile, modules_dir.parent))
            if locked.is_local_source
            else compute_package_hash(root)
        )
    except (OSError, ValueError, LocalResolutionError) as exc:
        raise ClaudeNativePluginError(
            f"Cannot verify native Claude source for {key}. "
            "Restore the locked source before retrying."
        ) from exc
    if observed != locked.content_hash:
        raise ClaudeNativePluginError(
            f"Native Claude source integrity changed for {key}; refusing registration."
        )


def resync_claude_plugins(
    *,
    modules_dir: Path,
    scope: Any,
    lockfile: Any,
    targets: list[Any] | None = None,
    retained_targets: list[Any] | None = None,
    prepared: dict[str, ClaudeNativePlugin] | None = None,
    allow_executables: dict[str, dict[str, bool]] | None = None,
    logger: Any = None,
    dry_run: bool = False,
) -> None:
    """Reconcile canonical locked survivors; never admit an unscanned new plugin.

    Install supplies freshly admitted plans. Uninstall retains only identities
    already owned, intersected with the surviving lockfile and its trust state.
    Owned identities that were not freshly admitted are retained against
    ``targets`` plus ``retained_targets`` (the declared universe), so a one-shot
    ``--target`` narrowing never retires them; ``None`` preserves Claude.
    """
    from apm_cli.copilot_plugins.registrar import candidates_from_lockfile
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan
    from apm_cli.install.exec_gate import check_executable_approval
    from apm_cli.install.target_filter import resolve_effective_package_targets
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType

    from .admission import plan_native_claude_plugin

    if not is_user_scope(scope) or dry_run:
        return
    modules_dir = Path(modules_dir).absolute()
    ledger = modules_dir / LEDGER_RELATIVE
    if not prepared and not ledger.exists():
        return
    if has_symlink_component(modules_dir, ledger):
        raise ClaudeNativePluginError("Refusing symlinked native Claude ownership ledger.")
    previous = _read_ledger(ledger, modules_dir)
    if not prepared and not previous:
        return
    if lockfile is None:
        raise ClaudeNativePluginError(
            "Native Claude reconciliation requires a readable canonical lockfile. "
            "Repair the lockfile before changing native registration."
        )
    if any(
        entry["dependency_key"] in lockfile.dependencies
        and not (modules_dir / entry["source"]).is_dir()
        for entry in previous.values()
    ):
        raise ClaudeNativePluginError(
            "A retained native Claude source is missing. Restore the pinned package "
            "before retrying; existing native ownership was preserved."
        )
    known_keys = {entry["dependency_key"] for entry in previous.values()} | set(prepared or {})
    active_targets = targets if targets is not None else [KNOWN_TARGETS["claude"]]
    retention_targets = list(
        {
            target.name: target
            for target in reversed(
                [
                    *active_targets,
                    *(
                        retained_targets
                        if retained_targets is not None
                        else [KNOWN_TARGETS["claude"]]
                    ),
                ]
            )
        }.values()
    )
    plans: list[ClaudeNativePlugin] = []
    for candidate in candidates_from_lockfile(lockfile, modules_dir):
        key = candidate.dependency_key
        if key not in known_keys:
            continue
        locked = lockfile.dependencies[key]
        root = candidate.install_path
        manifest = root / "apm.yml"
        package = APMPackage.from_apm_yml(manifest, source_path=root)
        info = PackageInfo(
            package=package,
            install_path=root,
            dependency_ref=locked.to_dependency_ref(),
            package_type=PackageType(locked.package_type),
        )
        selection = resolve_effective_package_targets(
            active_targets if prepared and key in prepared else retention_targets,
            candidate.target_subset,
            info,
            None,
            key,
        )
        hooks_approved, *_ = check_executable_approval(key, info, allow_executables)
        plan = plan_native_claude_plugin(
            info,
            package_name=key,
            targets=list(selection.targets),
            scope=scope,
            skill_subset=tuple(info.dependency_ref.skill_subset or ()) or None,
            hooks_approved=hooks_approved,
            skip_bin=True,
        )
        if plan is None:
            continue
        DeployableSourcePlan.create(
            info,
            list(selection.targets),
            skill_subset=None,
            hooks_approved=hooks_approved,
            canvas_approved=False,
            skip_bin=True,
            native_plugin_root=plan.source_root,
        )
        _verify_locked_source_hash(locked, lockfile, modules_dir, root, key)
        if prepared and key in prepared and prepared[key] != plan:
            raise ClaudeNativePluginError(
                "Native Claude source changed after deployment admission."
            )
        source_pin = _locked_source_pin(locked)
        prior = previous.get(f"{plan.name}@{MARKETPLACE_NAME}")
        if prior is not None:
            if not prior.get("bundle_hash"):
                _check_bundle_fingerprint(root, None)
            if not prepared or key not in prepared:
                _check_bundle_fingerprint(root, prior["bundle_hash"])
                if prior.get("source_pin") != source_pin:
                    raise ClaudeNativePluginError(
                        "Native Claude source pin changed without fresh admission. "
                        "Reinstall with the original target selection before retrying."
                    )
            elif (
                locked.source != "local"
                and prior.get("source_pin") == source_pin
                and prior["bundle_hash"] != plan.bundle_hash
            ):
                _check_bundle_fingerprint(root, prior["bundle_hash"])
        plans.append(replace(plan, source_pin=source_pin))
    if set(prepared or {}) - {plan.dependency_key for plan in plans}:
        raise ClaudeNativePluginError(
            "An admitted native Claude plugin is absent from the authorized locked set. "
            "Resolve the install's dependency or executable-approval errors before retrying."
        )
    synchronize_claude_plugins(
        modules_dir=modules_dir,
        scope=scope,
        plugins=plans,
        logger=logger,
        dry_run=dry_run,
    )
