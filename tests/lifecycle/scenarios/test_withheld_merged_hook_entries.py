"""A withheld package's hook entry in a merged config is kept, named and detected (#4798).

PROMISE: P-INV-I4 -- a closed executables gate never widens what is deployed;
what it cannot prove APM wrote it keeps, announces and detects.
PROMISE: P-OWN-executable-item-admission -- an entry in a merged hook config
(Claude ``.claude/settings.json``) that renders exactly as a withheld
package's hook is named unless an ownership record attributes it to another
source (source: install/withheld_merged_hooks.py).

Each row deploys h-hook's hook to Claude, closes the gate (``apm deny``), lets
install retire the entry, then puts an entry back the way a checkout or a
hand edit would. Install keeps the user's bytes and warns; ``apm audit --ci``
fails, naming the entry. A blank ownership marker attributes nothing; a
marker that is not a string, or a config reached through a symlink, cannot be
read as ownership, so the audit fails closed instead of skipping it. The
control row (the user's own hook in the same event) proves the scan does not
name unrelated entries.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world import use_policy
from tests.utils.artifact_snapshot import ArtifactSnapshotSet, assert_snapshot_set_unchanged
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I4", "P-OWN-executable-item-admission"),
]

_HOOK = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo h"}]}]}}
_SETTINGS = ".claude/settings.json"
_SIDECAR = ".claude/apm-hooks.json"
_ENTRY = f"{_SETTINGS}: SessionStart"


def _text(result) -> str:
    return " ".join(f"{result.stdout} {result.stderr}".split())


def _ok(world, *args: str):
    result = driver.run(world, list(args))
    assert result.returncode == 0, _text(result)
    return result


def _withheld_after_deploy(world) -> dict:
    """h-hook's hook deployed to Claude, then denied and retired; return the deployed config."""
    factory = LocalPackageFactory(world.root / "merged-hook")
    package = factory.create("h-hook", version="1.0.0")
    factory.add_hook(package, "session", _HOOK)
    use_policy(world, "allow")
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["targets"] = ["copilot", "claude"]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    root = str(package.root)
    _ok(world, "install", root)
    _ok(world, "approve", root)
    _ok(world, "install")
    settings = world.project / _SETTINGS
    deployed = json.loads(settings.read_text(encoding="utf-8"))
    assert deployed["hooks"]["SessionStart"], deployed
    _ok(world, "deny", root)
    _ok(world, "install")
    retired = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
    assert not (retired.get("hooks") or {}).get("SessionStart"), retired
    (world.project / _SIDECAR).unlink(missing_ok=True)
    return deployed


def _put_back(world, document: dict) -> bytes:
    data = json.dumps(document, indent=2).encode("ascii")
    path = world.project / _SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def _audit_unchanged(world):
    """``apm audit --ci`` is read-only: project and HOME bytes are identical after it."""
    roots = {"project": world.project, "home": world.home}
    before = ArtifactSnapshotSet.capture(roots)
    audit = driver.run(world, ["audit", "--ci"])
    assert_snapshot_set_unchanged(before, ArtifactSnapshotSet.capture(roots))
    return audit


def _marked(deployed: dict, marker: object) -> dict:
    document = json.loads(json.dumps(deployed))
    for entry in document["hooks"]["SessionStart"]:
        entry["_apm_source"] = marker
    return document


@pytest.mark.parametrize("attribution", ["unattributed", "blank-marker"])
def test_an_entry_matching_a_withheld_hook_is_kept_announced_and_detected(
    lifecycle_world, attribution
) -> None:
    """No marker, or a blank one, attributes nothing: install keeps and names it, audit fails."""
    world = lifecycle_world
    deployed = _withheld_after_deploy(world)
    document = deployed if attribution == "unattributed" else _marked(deployed, "  ")
    data = _put_back(world, document)

    install = driver.run(world, ["install"])
    text = _text(install)
    assert install.returncode == 0, text
    assert f"Kept hook entry {_ENTRY}" in text, text
    assert (world.project / _SETTINGS).read_bytes() == data

    audit = _audit_unchanged(world)
    text = _text(audit)
    assert audit.returncode == 1, text
    assert "hook-approval-pins" in text, text
    assert f"{_ENTRY}: hook entry of" in text, text
    assert "is not attributed to another source" in text, text


@pytest.mark.parametrize("unreadable", ["non-string-marker", "symlinked-config"])
def test_an_entry_whose_ownership_cannot_be_read_fails_the_audit_closed(
    lifecycle_world, unreadable
) -> None:
    """Ownership APM cannot read never excuses an entry: ``audit --ci`` fails naming why."""
    world = lifecycle_world
    deployed = _withheld_after_deploy(world)
    if unreadable == "non-string-marker":
        _put_back(world, _marked(deployed, {"not": "a marker"}))
        reason = "invalid ownership metadata"
    else:
        if os.name == "nt":
            pytest.skip("creating a symlink needs privileges on Windows")
        external = Path(world.root) / "external-settings.json"
        external.write_bytes(json.dumps(deployed).encode("ascii"))
        settings = world.project / _SETTINGS
        settings.unlink(missing_ok=True)
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.symlink_to(external)
        reason = "reached through a symlink"

    audit = _audit_unchanged(world)
    text = _text(audit)
    assert audit.returncode == 1, text
    assert "hook-approval-pins" in text, text
    assert "its hook paths cannot be checked" in text, text
    assert reason in text, text


def test_the_users_own_hook_in_the_same_event_is_not_named(lifecycle_world) -> None:
    """Control: an entry that does not render as h-hook's hook is the user's; audit passes."""
    world = lifecycle_world
    deployed = _withheld_after_deploy(world)
    document = json.loads(json.dumps(deployed))
    for entry in document["hooks"]["SessionStart"]:
        entry["hooks"] = [{"type": "command", "command": "echo mine"}]
    data = _put_back(world, document)

    install = driver.run(world, ["install"])
    assert install.returncode == 0, _text(install)
    assert "Kept hook entry" not in _text(install), _text(install)
    assert (world.project / _SETTINGS).read_bytes() == data
    audit = _audit_unchanged(world)
    assert audit.returncode == 0, _text(audit)
