"""Ownership-aware Codex registration through the qualified public native API.

The journal owns transport metadata, never native settings or cache files.
Pending entries retain both prior and intended source identities so a failed
native operation can be retried without adopting another owner's registration.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from apm_cli.codex_plugins.admission import CodexNativeDeployment
from apm_cli.codex_plugins.client import (
    CodexNativeClient,
    CodexNativeError,
    CodexRpc,
    NativeUserConfig,
)
from apm_cli.codex_plugins.snapshot import NativeSnapshot, prepare_snapshot, repair_generation
from apm_cli.core.scope import InstallScope
from apm_cli.deps.plugin_parser import _bounded_read_json
from apm_cli.utils.atomic_io import atomic_write_text
from apm_cli.utils.content_hash import compute_native_bundle_hash
from apm_cli.utils.path_security import ensure_path_within, has_symlink_component, safe_rmtree


def is_codex_source_pin(value: object) -> bool:
    """Return whether a value is a full commit identity, the only valid source pin."""
    return (
        isinstance(value, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is not None
    )


def marketplace_name(modules_dir: Path, dependency_key: str) -> str:
    """Namespace an owned marketplace by APM home and canonical dependency identity."""
    identity = f"{modules_dir.resolve().as_posix()}\0{dependency_key}".encode()
    return "apm-" + hashlib.sha256(identity).hexdigest()[:24]


def ledger_path_for(modules_dir: Path) -> Path:
    """Keep ownership outside the disposable package materialization directory."""
    return modules_dir.parent / ".codex-native" / "ownership.json"


def _safe_owned_path(path: Path, root: Path) -> Path:
    ensure_path_within(path, root)
    if has_symlink_component(root.parent, path):
        raise CodexNativeError(f"Codex transport ownership contains a symlink; inspect {path}.")
    return path


def _snapshot_data(snapshot: NativeSnapshot) -> dict[str, str]:
    return {
        "path": str(snapshot.path),
        "content_commit": snapshot.content_commit,
        "catalog_commit": snapshot.catalog_commit,
    }


def _read_snapshot(data: Any, root: Path) -> NativeSnapshot:
    if (
        not isinstance(data, dict)
        or set(data) != {"path", "content_commit", "catalog_commit"}
        or not all(isinstance(value, str) for value in data.values())
        or any(
            re.fullmatch(r"[0-9a-f]{40}", data[key]) is None
            for key in ("content_commit", "catalog_commit")
        )
    ):
        raise CodexNativeError(
            f"Invalid Codex transport ownership; inspect {root / 'ownership.json'} before retrying."
        )
    path = _safe_owned_path(Path(data["path"]), root / "snapshots")
    return NativeSnapshot(path, data["content_commit"], data["catalog_commit"])


@dataclass(frozen=True)
class _Record:
    name: str
    source_hash: str
    snapshot: NativeSnapshot
    config_path: Path
    previous: NativeSnapshot | None = None
    marketplace_path: Path | None = None
    installed_root: Path | None = None
    pending: bool = True
    source_commit: str | None = None

    def data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_hash": self.source_hash,
            "snapshot": _snapshot_data(self.snapshot),
            "config_path": str(self.config_path),
            "previous": _snapshot_data(self.previous) if self.previous else None,
            "marketplace_path": str(self.marketplace_path) if self.marketplace_path else None,
            "installed_root": str(self.installed_root) if self.installed_root else None,
            "pending": self.pending,
            "source_commit": self.source_commit,
        }

    @classmethod
    def read(cls, data: Any, root: Path) -> _Record:
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("name"), str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", data["name"]) is None
            or not isinstance(data.get("source_hash"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", data["source_hash"]) is None
            or not isinstance(data.get("config_path"), str)
            or not isinstance(data.get("pending"), bool)
            or (
                data.get("source_commit") is not None
                and (not is_codex_source_pin(data["source_commit"]))
            )
            or any(
                data.get(key) is not None and not isinstance(data[key], str)
                for key in ("marketplace_path", "installed_root")
            )
        ):
            raise CodexNativeError(
                f"Invalid Codex ownership record; inspect {root / 'ownership.json'} before retrying."
            )
        return cls(
            data["name"],
            data["source_hash"],
            _read_snapshot(data.get("snapshot"), root),
            Path(data["config_path"]),
            _read_snapshot(data["previous"], root) if data.get("previous") else None,
            Path(data["marketplace_path"]) if data.get("marketplace_path") else None,
            Path(data["installed_root"]) if data.get("installed_root") else None,
            data["pending"],
            data.get("source_commit"),
        )


def _read_records(path: Path) -> dict[str, _Record]:
    _safe_owned_path(path, path.parent)
    if not path.exists():
        return {}
    data = _bounded_read_json(path)
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("plugins"), dict)
    ):
        raise CodexNativeError(f"Invalid Codex ownership journal; inspect {path} before retrying.")
    return {key: _Record.read(value, path.parent) for key, value in data["plugins"].items()}


def owns_codex_dependency(
    modules_dir: Path,
    dependency_key: str,
    *,
    ownership_cache: dict[Path, frozenset[str]] | None = None,
) -> bool:
    """Read durable native identity for cold-cache preflight without launching Codex."""
    keys = ownership_cache.get(modules_dir) if ownership_cache is not None else None
    if keys is None:
        keys = frozenset(_read_records(ledger_path_for(modules_dir)))
        if ownership_cache is not None:
            ownership_cache[modules_dir] = keys
    return dependency_key in keys


def verify_codex_source_identity(
    modules_dir: Path,
    *,
    dependency_key: str,
    source_hash: str,
    source_commit: str | None,
    candidate: Path | None = None,
    records: dict[str, _Record] | None = None,
) -> dict[str, _Record]:
    """Reject cache drift without rebasing an immutable pin onto changed publisher bytes."""
    if records is None:
        records = _read_records(ledger_path_for(modules_dir))
    record = records.get(dependency_key)
    if source_commit is not None:
        if not is_codex_source_pin(source_commit):
            raise CodexNativeError(
                f"Invalid original Codex source pin for {dependency_key}; "
                "restore the pinned dependency before retrying."
            )
        if record is not None and record.source_commit is None:
            raise CodexNativeError(
                f"Codex native source provenance is missing for {dependency_key}. "
                "Save its pinned declaration, then use "
                f"'apm uninstall -g {dependency_key}' and "
                "'apm install -g <saved-pinned-dependency> --target codex'; "
                "native state was retained."
            )
    if (
        candidate is not None
        and (not candidate.is_dir() or compute_native_bundle_hash(candidate) != source_hash)
    ) or (
        source_commit is not None
        and record is not None
        and record.source_commit == source_commit
        and record.source_hash != source_hash
    ):
        raise CodexNativeError(
            f"Codex native cache differs from its admitted source for {dependency_key}. "
            "Remove only this dependency's disposable apm_modules directory and run "
            "'apm install -g --frozen --target codex'; native state was retained."
        )
    return records


def _write_records(path: Path, records: dict[str, _Record]) -> None:
    _safe_owned_path(path, path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(
            {
                "version": 1,
                "plugins": {key: record.data() for key, record in sorted(records.items())},
            },
            indent=2,
        )
        + "\n",
    )


def _marketplace(snapshot: NativeSnapshot) -> dict[str, str]:
    return {"source_type": "git", "source": snapshot.path.as_uri(), "ref": snapshot.catalog_commit}


def _table(config: NativeUserConfig, key: str) -> dict[str, Any]:
    value = config.values.get(key, {})
    if not isinstance(value, dict):
        raise CodexNativeError("Invalid native configuration table; inspect Codex configuration.")
    return value


def _preflight(
    config: NativeUserConfig,
    marketplace: str,
    name: str,
    record: _Record | None,
    *,
    removing: bool = False,
) -> None:
    if any(
        key.endswith(f"@{marketplace}") and key != f"{name}@{marketplace}"
        for key in _table(config, "plugins")
    ):
        raise CodexNativeError(
            "Codex native ownership collision: another plugin uses the owned marketplace. "
            "Retire that registration using Codex before retrying; native state was retained."
        )
    actual_marketplace = _table(config, "marketplaces").get(marketplace)
    actual_plugin = _table(config, "plugins").get(f"{name}@{marketplace}")
    if record is None:
        if actual_marketplace is not None or actual_plugin is not None:
            raise CodexNativeError(
                "Codex native ownership collision. Existing settings were retained; "
                "remove the conflicting registration yourself or use its original owner."
            )
        return
    expected = [_marketplace(record.snapshot)]
    if record.previous:
        expected.append(_marketplace(record.previous))
    if config.path.resolve() != record.config_path.resolve() or (
        actual_marketplace is not None and actual_marketplace not in expected
    ):
        raise CodexNativeError(
            "Codex native ownership changed. APM retained the registration; "
            "restore the recorded source or remove it using Codex before retrying."
        )
    if actual_plugin is not None and (
        not isinstance(actual_plugin, dict)
        or not isinstance(actual_plugin.get("enabled"), bool)
        or (removing and actual_plugin != {"enabled": False})
    ):
        raise CodexNativeError(
            "Codex plugin has user-managed settings. APM retained it rather than "
            "discard activation or MCP policy; remove it explicitly using Codex and retry."
        )


def _observed_root(
    detail: dict[str, Any],
    deployment: CodexNativeDeployment,
    record: _Record,
    marketplace: str,
) -> Path:
    """Derive installed root only from a selected public native skill path."""
    summary = detail.get("summary", {})
    if (
        not isinstance(summary, dict)
        or summary.get("id") != f"{record.name}@{marketplace}"
        or summary.get("installed") is not True
        or not isinstance(summary.get("source"), dict)
        or summary["source"].get("type") != "git"
        or summary["source"].get("url") != record.snapshot.path.as_uri()
        or summary["source"].get("sha") != record.snapshot.content_commit
    ):
        raise CodexNativeError("Codex did not load the owned source pin; rerun apm install.")
    skills = detail.get("skills")
    native_mcp = detail.get("mcpServers")
    if (
        not isinstance(native_mcp, list)
        or not all(isinstance(name, str) for name in native_mcp)
        or sorted(native_mcp) != list(deployment.plugin.mcp_server_names)
    ):
        raise CodexNativeError(
            "Codex did not discover every declared MCP server. Inspect the native "
            "MCP configuration and retry; APM will not claim a complete native load."
        )
    if not isinstance(skills, list):
        raise CodexNativeError(
            "Codex did not expose installed skill paths; native loading is unqualified."
        )
    relatives = [
        Path(path) for path in deployment.source_plan.paths if Path(path).name == "SKILL.md"
    ]
    roots: set[Path] = set()
    for skill in skills:
        path_value = skill.get("path") if isinstance(skill, dict) else None
        if not isinstance(path_value, str) or not Path(path_value).is_absolute():
            continue
        path = Path(path_value)
        for relative in relatives:
            if path.parts[-len(relative.parts) :] == relative.parts:
                root = path.parents[len(relative.parts) - 1]
                ensure_path_within(path, root)
                if has_symlink_component(root.parent, path):
                    raise CodexNativeError(
                        "Native installed path contains a symlink; inspect Codex state."
                    )
                roots.add(root)
    if len(roots) != 1:
        raise CodexNativeError(
            "This Codex bundle has no unambiguous public installed skill path. "
            "The qualified route requires a native-discoverable skill; no complete-load claim was made."
        )
    root = roots.pop()
    if any(
        root.resolve().is_relative_to(source.resolve())
        for source in (
            deployment.plugin.source_root,
            record.snapshot.path,
        )
    ):
        raise CodexNativeError("Codex returned source content instead of its installed copy.")
    if compute_native_bundle_hash(root) != deployment.source_hash:
        raise CodexNativeError(
            "Codex installed bundle is incomplete or changed; rerun apm install."
        )
    return root


def _remove(
    client: CodexNativeClient,
    rpc: CodexRpc,
    config: NativeUserConfig,
    marketplace: str,
    record: _Record,
) -> None:
    _preflight(config, marketplace, record.name, record, removing=True)
    plugin_id = f"{record.name}@{marketplace}"
    if plugin_id in _table(config, "plugins"):
        client.remove_plugin(rpc, plugin_id)
    current = client.read_config(rpc)
    _preflight(current, marketplace, record.name, record, removing=True)
    if marketplace in _table(current, "marketplaces"):
        client.remove_marketplace(rpc, marketplace)


def _refresh(
    client: CodexNativeClient,
    rpc: CodexRpc,
    deployment: CodexNativeDeployment,
    record: _Record,
    marketplace: str,
    config: NativeUserConfig,
) -> _Record:
    edits: list[dict[str, Any]] = []
    plugin_id = f"{record.name}@{marketplace}"
    if plugin_id not in _table(config, "plugins"):
        edits.append({"keyPath": f"plugins.{json.dumps(plugin_id)}", "value": {"enabled": False}})
    if _table(config, "marketplaces").get(marketplace) != _marketplace(record.snapshot):
        edits.append(
            {
                "keyPath": f"marketplaces.{json.dumps(marketplace)}",
                "value": _marketplace(record.snapshot),
            }
        )
    if edits:
        client.write_config(rpc, edits, config.version)
    roots = client.upgrade(rpc, marketplace)
    if len(roots) > 1:
        raise CodexNativeError(
            "Codex refreshed multiple roots for one marketplace; inspect native state."
        )
    manifest = roots[0] / ".agents/plugins/marketplace.json" if roots else record.marketplace_path
    if manifest is None:
        raise CodexNativeError("Codex did not expose its installed marketplace; rerun apm install.")
    detail = client.read_plugin(rpc, manifest, record.name)
    root = _observed_root(detail, deployment, record, marketplace)
    _preflight(client.read_config(rpc), marketplace, record.name, record)
    return replace(record, marketplace_path=manifest, installed_root=root, pending=False)


def synchronize_codex_plugins(
    deployments: Sequence[CodexNativeDeployment],
    *,
    modules_dir: Path,
    cwd: Path,
    scope: InstallScope,
    retained_keys: set[str],
) -> tuple[str, ...]:
    """Reconcile only admitted bundles and journal-owned native registrations."""
    ledger = ledger_path_for(modules_dir)
    records = _read_records(ledger)
    if not deployments and set(records).issubset(retained_keys):
        return ()
    desired = {item.plugin.dependency_key: item for item in deployments}
    for key, item in desired.items():
        verify_codex_source_identity(modules_dir, records=records, **item.plugin.source_identity())
        if key in records and records[key].name != item.plugin.name:
            raise CodexNativeError(
                "Codex native plugin name changed. Uninstall the prior dependency before "
                "installing its renamed version; its registration was retained."
            )
    client = CodexNativeClient.for_scope(scope, cwd)
    changed: list[str] = []
    with client.connect() as rpc:
        config = client.read_config(rpc)
        for key, record in records.items():
            item = desired.get(key)
            removing = key not in retained_keys or (
                item is not None and item.plugin.name != record.name
            )
            if item is not None or removing:
                _preflight(
                    config,
                    marketplace_name(modules_dir, key),
                    record.name,
                    record,
                    removing=removing,
                )
        for key, item in desired.items():
            if key not in records:
                _preflight(config, marketplace_name(modules_dir, key), item.plugin.name, None)
        for key, record in list(records.items()):
            item = desired.get(key)
            if key in retained_keys and (item is None or item.plugin.name == record.name):
                continue
            _remove(
                client, rpc, client.read_config(rpc), marketplace_name(modules_dir, key), record
            )
            for snapshot in (record.snapshot, record.previous):
                if snapshot and snapshot.path.exists():
                    safe_rmtree(snapshot.path, ledger.parent / "snapshots")
            del records[key]
            _write_records(ledger, records)
            changed.append(key)
        for key, item in desired.items():
            record = records.get(key)
            name = marketplace_name(modules_dir, key)
            config = client.read_config(rpc)
            _preflight(config, name, item.plugin.name, record)
            healthy = (
                record is not None
                and not record.pending
                and record.source_hash == item.source_hash
                and record.installed_root is not None
                and record.marketplace_path is not None
                and record.marketplace_path.is_file()
                and record.snapshot.path.is_dir()
                and _table(config, "marketplaces").get(name) == _marketplace(record.snapshot)
                and f"{record.name}@{name}" in _table(config, "plugins")
                and compute_native_bundle_hash(record.installed_root) == item.source_hash
            )
            if healthy:
                detail = client.read_plugin(rpc, record.marketplace_path, record.name)
                _observed_root(detail, item, record, name)
                if record.source_commit != item.plugin.source_commit:
                    records[key] = replace(record, source_commit=item.plugin.source_commit)
                    _write_records(ledger, records)
                continue
            if (
                record is None
                or record.source_hash != item.source_hash
                or not record.snapshot.path.is_dir()
            ):
                destination = _safe_owned_path(
                    ledger.parent / "snapshots" / uuid.uuid4().hex,
                    ledger.parent,
                )
                snapshot = prepare_snapshot(
                    item.source_plan,
                    destination,
                    name,
                    item.plugin.name,
                    expected_hash=item.source_hash,
                )
                record = _Record(
                    item.plugin.name,
                    item.source_hash,
                    snapshot,
                    config.path,
                    source_commit=item.plugin.source_commit,
                    previous=(
                        record.previous
                        if record
                        and record.previous
                        and _table(config, "marketplaces").get(name)
                        == _marketplace(record.previous)
                        else record.snapshot
                        if record
                        else None
                    ),
                )
            elif not record.pending:
                record = replace(
                    record,
                    snapshot=repair_generation(record.snapshot),
                    previous=record.snapshot,
                    pending=True,
                )
            record = replace(record, source_commit=item.plugin.source_commit)
            records[key] = record
            _write_records(ledger, records)
            record = _refresh(client, rpc, item, record, name, config)
            if (
                record.previous
                and record.previous.path != record.snapshot.path
                and record.previous.path.exists()
            ):
                safe_rmtree(record.previous.path, ledger.parent / "snapshots")
            records[key] = replace(record, previous=None)
            _write_records(ledger, records)
            changed.append(key)
    return tuple(changed)


def remove_codex_plugins(
    *,
    modules_dir: Path,
    dependency_keys: set[str],
    cwd: Path,
    scope: InstallScope,
) -> None:
    """Retire selected native registrations before uninstall releases APM ownership."""
    records = _read_records(ledger_path_for(modules_dir))
    if not dependency_keys.intersection(records):
        return
    synchronize_codex_plugins(
        [],
        modules_dir=modules_dir,
        cwd=cwd,
        scope=scope,
        retained_keys=set(records) - dependency_keys,
    )
