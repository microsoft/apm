"""A local bundle's canvas extension follows the executables gate like its hooks and MCP.

Once deployed, a reinstall that the gate no longer admits (approval revoked
or the exact bundle denied) removes the extension and its lock claim. The
pending record lists canvas, so ``apm approve <key>`` grants it. A plain
``apm install`` re-checks every deployed bundle extension as it re-checks
bundle hooks, and ``apm audit --ci`` reports one trust no longer admits. A
blocked bundle never retires an extension another bundle deployed. A
reinstall retires the extensions the bundle's new version no longer ships.
Committed bundle records and lock rows that name paths outside the target's
extension or hook directory are never acted on.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from apm_cli.bundle.local_bundle import detect_local_bundle
from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockFile
from apm_cli.security.executables import local_bundle_approval_key
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._marketplace_pack_kit import (
    MarketplacePackWorld,
    output,
    skill_md,
    write,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.promise("P-INV-I4"),
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_EXTENSION = ".github/extensions/board/extension.mjs"


def _pack(
    world: MarketplacePackWorld,
    root: Path,
    out: Path,
    name: str = "canvaspkg",
    *,
    version: str = "1.0.0",
    extension: str | None = "board",
) -> Path:
    write(
        root / "apm.yml",
        f"name: {name}\nversion: {version}\ndescription: c\nlicense: MIT\n"
        "targets: [copilot]\ndependencies:\n  apm: []\n",
    )
    write(root / ".apm" / "skills" / f"{name}-s" / "SKILL.md", skill_md(f"{name}-s"))
    shutil.rmtree(root / ".apm" / "extensions", ignore_errors=True)
    if extension is not None:
        write(
            root / ".apm" / "extensions" / extension / "extension.mjs",
            f"export default {{ name: '{name}' }};\n",
        )
    (root / ".github").mkdir(exist_ok=True)
    world.apm(root, "install", "--no-policy")
    world.apm(root, "pack", "-o", str(out))
    return out / f"{name}-{version}"


def _approval_key(bundle: Path) -> str:
    info = detect_local_bundle(bundle)
    return local_bundle_approval_key(
        info.package_id, str(info.plugin_json.get("version") or ""), info.source_dir, info.lockfile
    )


def _set_executables(project: Path, executables: dict | None) -> None:
    manifest = load_yaml(project / "apm.yml")
    manifest.pop("executables", None)
    if executables is not None:
        manifest["executables"] = executables
    dump_yaml(manifest, project / "apm.yml")


def _claimed(world: MarketplacePackWorld, project: Path) -> bool:
    return _EXTENSION in (world.lock(project).get("local_deployed_files") or [])


def test_bundle_canvas_retires_on_revoke_and_deny(tmp_path: Path, apm_binary_path: Path) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    world.apm(world.isolated.work_root, "experimental", "enable", "canvas")
    bundle = _pack(world, world.isolated.work_root / "canvaspkg", tmp_path / "out")
    assert (bundle / "extensions/board/extension.mjs").is_file()
    key = _approval_key(bundle)
    consumer = world.consumer("bundle-consumer", targets=("copilot",))
    install = ("install", str(bundle), "--no-policy")

    # Gate off: the extension deploys and is claimed.
    world.apm(consumer, *install)
    assert (consumer / _EXTENSION).is_file()
    assert _claimed(world, consumer)

    # Gate on without approval: the reinstall retires the deployed extension.
    _set_executables(consumer, {"allow": {}})
    world.apm(consumer, *install)
    assert not (consumer / _EXTENSION).exists()
    assert not _claimed(world, consumer)

    # The pending record names canvas, so approving the key grants it.
    pending = output(world.apm(consumer, "approve", "--pending"))
    assert "canvas" in pending, pending
    approved = output(world.apm(consumer, "approve", key))
    assert "canvas" in approved, approved
    world.apm(consumer, *install)
    assert (consumer / _EXTENSION).is_file()
    assert _claimed(world, consumer)

    # Denying the exact bundle retires it again.
    world.apm(consumer, "deny", key)
    world.apm(consumer, *install)
    assert not (consumer / _EXTENSION).exists()
    assert not _claimed(world, consumer)
    world.apm(consumer, "audit", "--ci", "--no-policy")


def test_plain_install_retires_denied_bundle_canvas_and_audit_reports_it(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    world.apm(world.isolated.work_root, "experimental", "enable", "canvas")
    bundle = _pack(world, world.isolated.work_root / "canvaspkg", tmp_path / "out")
    key = _approval_key(bundle)
    consumer = world.consumer("bundle-consumer", targets=("copilot",))
    _set_executables(consumer, {"allow": {key: {"canvas": True}}})
    world.apm(consumer, "install", str(bundle), "--no-policy")
    assert (consumer / _EXTENSION).is_file()
    world.apm(consumer, "audit", "--ci", "--no-policy")

    world.apm(consumer, "deny", key)
    stale = world.apm(consumer, "audit", "--ci", "--no-policy", expect=None)
    assert stale.returncode != 0, output(stale)
    assert "canvas" in output(stale) and "canvaspkg" in output(stale), output(stale)

    # A plain install, with no bundle argument, retires it.
    world.apm(consumer, "install", "--no-policy")
    assert not (consumer / _EXTENSION).exists()
    assert not _claimed(world, consumer)
    world.apm(consumer, "audit", "--ci", "--no-policy")


def test_blocked_bundle_keeps_another_bundles_extension(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    world.apm(world.isolated.work_root, "experimental", "enable", "canvas")
    first = _pack(world, world.isolated.work_root / "first", tmp_path / "out-a", "first")
    second = _pack(world, world.isolated.work_root / "second", tmp_path / "out-b", "second")
    consumer = world.consumer("bundle-consumer", targets=("copilot",))
    _set_executables(consumer, {"allow": {_approval_key(first): {"canvas": True}}})
    world.apm(consumer, "install", str(first), "--no-policy")
    before = (consumer / _EXTENSION).read_bytes()

    # The second bundle ships the same extension name and is not approved.
    world.apm(consumer, "install", str(second), "--no-policy")

    assert (consumer / _EXTENSION).read_bytes() == before
    assert _claimed(world, consumer)


def test_reinstall_retires_extensions_the_new_version_no_longer_ships(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    world.apm(world.isolated.work_root, "experimental", "enable", "canvas")
    root = world.isolated.work_root / "canvaspkg"
    consumer = world.consumer("bundle-consumer", targets=("copilot",))
    panel = ".github/extensions/panel/extension.mjs"
    install = ("install", "--no-policy")

    world.apm(consumer, install[0], str(_pack(world, root, tmp_path / "v1")), install[1])
    assert (consumer / _EXTENSION).is_file()

    # Version 2 renames the extension: the old one is retired, the new one lands.
    v2 = _pack(world, root, tmp_path / "v2", version="2.0.0", extension="panel")
    world.apm(consumer, install[0], str(v2), install[1])
    assert not (consumer / _EXTENSION).exists()
    assert not _claimed(world, consumer)
    assert (consumer / panel).is_file()

    # Version 3 ships no canvas at all: nothing of the bundle's canvas remains.
    v3 = _pack(world, root, tmp_path / "v3", version="3.0.0", extension=None)
    world.apm(consumer, install[0], str(v3), install[1])
    assert not (consumer / panel).exists()
    assert panel not in (world.lock(consumer).get("local_deployed_files") or [])
    world.apm(consumer, "audit", "--ci", "--no-policy")


def test_recheck_never_removes_files_outside_the_bundle_roots(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    world.apm(world.isolated.work_root, "experimental", "enable", "canvas")
    consumer = world.consumer("bundle-consumer", targets=("copilot",))
    _set_executables(consumer, {"allow": {}})
    victims = {name: consumer.parent / f"{name}.txt" for name in ("row", "canvas", "hook")}
    for victim in victims.values():
        victim.write_text("keep me\n", encoding="utf-8")
    # Committed state names files outside the project: a lock row that climbs
    # out of the extension root, and records listing absolute paths.
    escaping = ".github/extensions/../../../row.txt"
    rows = [escaping, str(victims["canvas"]), str(victims["hook"])]
    lockfile = LockFile()
    DeploymentLedgerCodec.record_local_bundle_files(
        lockfile,
        rows,
        {
            escaping: compute_file_hash(victims["row"]),
            **{row: compute_file_hash(Path(row)) for row in rows[1:]},
        },
        scope=InstallScope.PROJECT,
    )
    lockfile.write(consumer / "apm.lock.yaml")
    marker = "local-bundle::crafted"
    (consumer / ".github" / "hooks").mkdir(parents=True, exist_ok=True)
    (consumer / ".github" / "apm-hooks.json").write_text(
        json.dumps(
            {
                "local-bundle-canvas": [
                    {"_apm_source": marker, "canvas_files": [str(victims["canvas"])]}
                ],
                "local-bundle-hooks": [
                    {"_apm_source": marker, "hook_files": [str(victims["hook"])]}
                ],
            }
        ),
        encoding="utf-8",
    )

    world.apm(consumer, "install", "--no-policy", expect=None)

    for victim in victims.values():
        assert victim.read_text(encoding="utf-8") == "keep me\n", victim
