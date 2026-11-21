"""A local bundle's canvas extension follows the executables gate like its hooks and MCP.

Once deployed, a reinstall that the gate no longer admits (approval revoked
or the exact bundle denied) removes the extension and its lock claim. The
pending record lists canvas, so ``apm approve <key>`` grants it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.bundle.local_bundle import detect_local_bundle
from apm_cli.security.executables import local_bundle_approval_key
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._marketplace_pack_kit import (
    MarketplacePackWorld,
    output,
    skill_md,
    write,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.promise("P-INV-I4"),
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_EXTENSION = ".github/extensions/board/extension.mjs"


def _pack(world: MarketplacePackWorld, root: Path, out: Path) -> Path:
    write(
        root / "apm.yml",
        "name: canvaspkg\nversion: 1.0.0\ndescription: c\nlicense: MIT\n"
        "targets: [copilot]\ndependencies:\n  apm: []\n",
    )
    write(root / ".apm" / "skills" / "cs" / "SKILL.md", skill_md("cs"))
    write(root / ".apm" / "extensions" / "board" / "extension.mjs", "export default {};\n")
    (root / ".github").mkdir(exist_ok=True)
    world.apm(root, "install", "--no-policy")
    world.apm(root, "pack", "-o", str(out))
    return out / "canvaspkg-1.0.0"


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
