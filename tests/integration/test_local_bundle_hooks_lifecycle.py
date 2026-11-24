"""Local plugin bundle installs register hooks like a source install (#3700).

``apm pack`` emits a plugin bundle whose hooks live in ``hooks.json`` plus
``hooks/`` resources. ``apm install <bundle>`` must route them through the
hook integrator a source install uses, behind the exact-content executables
gate, keep ``apm audit --ci`` green, and keep them when an unrelated
dependency is installed or uninstalled.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.bundle.local_bundle import detect_local_bundle
from apm_cli.security.executables import local_bundle_approval_key
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._marketplace_pack_kit import (
    MarketplacePackWorld,
    output,
    skill_md,
    tree_bytes,
    write,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]


def _hooked_package(root: Path, name: str, matcher: str, skill: str) -> Path:
    write(
        root / "apm.yml",
        f"name: {name}\nversion: 1.0.0\ndescription: h\nlicense: MIT\ntargets: [claude, copilot]\n"
        "dependencies:\n  apm: []\n",
    )
    write(root / ".apm" / "skills" / skill / "SKILL.md", skill_md(skill))
    hook = {
        "hooks": {
            "PreToolUse": [
                {"matcher": matcher, "hooks": [{"type": "command", "command": "./scripts/x.sh"}]}
            ]
        }
    }
    write(root / ".apm" / "hooks" / "hooks.json", json.dumps(hook))
    script = write(root / ".apm" / "hooks" / "scripts" / "x.sh", f"#!/bin/sh\necho {name}\n")
    script.chmod(0o755)
    (root / ".claude").mkdir()
    return root


def _allow(project: Path, *keys: str) -> None:
    manifest = load_yaml(project / "apm.yml")
    manifest["executables"] = {"allow": {key: {"hooks": True} for key in keys}}
    dump_yaml(manifest, project / "apm.yml")


def _packed_bundle(world: MarketplacePackWorld, tmp_path: Path) -> tuple[Path, Path, str]:
    producer = _hooked_package(world.isolated.work_root / "hookpkg", "hookpkg", "Bash", "hs")
    world.apm(producer, "install", "--no-policy")
    world.apm(producer, "pack", "-o", str(tmp_path / "out"))
    bundle = tmp_path / "out" / "hookpkg-1.0.0"
    info = detect_local_bundle(bundle)
    key = local_bundle_approval_key(
        info.package_id, str(info.plugin_json.get("version") or ""), info.source_dir, info.lockfile
    )
    return producer, bundle, key


def _repack(
    world: MarketplacePackWorld, producer: Path, tmp_path: Path, version: str, script: str | None
) -> tuple[Path, str]:
    """Repack *producer* as *version*; ``script=None`` drops its hooks."""
    manifest = re.sub(r"version: \S+", f"version: {version}", (producer / "apm.yml").read_text())
    write(producer / "apm.yml", manifest)
    if script is None:
        shutil.rmtree(producer / ".apm" / "hooks")
    else:
        write(producer / ".apm" / "hooks" / "scripts" / "x.sh", script).chmod(0o755)
    world.apm(producer, "install", "--no-policy")
    world.apm(producer, "pack", "-o", str(tmp_path / "out"))
    bundle = tmp_path / "out" / f"hookpkg-{version}"
    info = detect_local_bundle(bundle)
    return bundle, local_bundle_approval_key(
        info.package_id, str(info.plugin_json.get("version") or ""), info.source_dir, info.lockfile
    )


def _hook_entries(project: Path) -> dict[str, list[str]]:
    path = project / ".claude" / "settings.json"
    if not path.exists():
        return {}
    settings = json.loads(path.read_text())
    return {
        event: [entry["matcher"] for entry in entries]
        for event, entries in settings.get("hooks", {}).items()
    }


def _run_hook(world: MarketplacePackWorld, project: Path, matcher: str) -> str:
    settings = json.loads((project / ".claude" / "settings.json").read_text())
    (command,) = [
        hook["command"]
        for entry in settings["hooks"]["PreToolUse"]
        if entry["matcher"] == matcher
        for hook in entry["hooks"]
    ]
    ran = subprocess.run(
        ["sh", "-c", command],
        cwd=project,
        env={**world.env, "CLAUDE_PROJECT_DIR": str(project)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert ran.returncode == 0, ran.stderr
    return ran.stdout


def _assert_lock_matches_disk(world: MarketplacePackWorld, project: Path) -> None:
    """I1: the bundle's recorded files match disk, and every deployed bundle script is recorded."""
    from apm_cli.utils.content_hash import compute_file_hash

    hashes = world.lock(project)["local_deployed_file_hashes"]
    assert hashes, "the lock records no local bundle files"
    for rel, digest in hashes.items():
        assert compute_file_hash(project / rel) == digest, rel
    scripts = {
        path.relative_to(project).as_posix()
        for path in (project / ".claude" / "hooks" / "hookpkg").rglob("*")
        if path.is_file()
    }
    assert scripts <= set(hashes), scripts - set(hashes)


def test_bundle_hooks_park_then_register_like_a_source_install(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    producer, bundle, key = _packed_bundle(world, tmp_path)

    # Source install of the same package: the reference registration.
    source = world.consumer("source-consumer", targets=("claude",))
    world.apm(source, "install", str(producer), "--no-policy")
    world.apm(source, "approve", str(producer))
    world.apm(source, "install", "--no-policy")
    assert _hook_entries(source) == {"PreToolUse": ["Bash"]}

    # executables: {} parks bundle hooks, naming the exact approval key.
    consumer = world.consumer("bundle-consumer", targets=("claude",))
    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {}
    dump_yaml(manifest, consumer / "apm.yml")
    text = output(world.apm(consumer, "install", str(bundle), "--no-policy"))
    assert "Skipped 2 bundle hook file(s) from hookpkg" in text, text
    assert "local bundle install does not register plugin hooks" not in text
    assert not (consumer / ".claude" / "settings.json").exists()
    assert not (consumer / ".claude" / "hooks").exists()
    assert (consumer / ".claude" / "skills" / "hs" / "SKILL.md").is_file()

    # Approved: a dry run still writes no hook config or scripts.
    _allow(consumer, key)
    world.apm(consumer, "install", str(bundle), "--no-policy", "--dry-run")
    assert not (consumer / ".claude" / "settings.json").exists()
    assert not (consumer / ".claude" / "hooks").exists()

    # Approved: registered through the same merge path as the source install.
    world.apm(consumer, "install", str(bundle), "--no-policy")
    assert _hook_entries(consumer) == _hook_entries(source)
    assert _run_hook(world, consumer, "Bash") == _run_hook(world, source, "Bash") == "hookpkg\n"
    sidecar = json.loads((consumer / ".claude" / "apm-hooks.json").read_text())
    assert [e["_apm_source"] for e in sidecar["PreToolUse"]] == [f"local-bundle::hookpkg::{key}"]
    assert list(consumer.rglob("*.sh.json")) == []
    lock = world.lock(consumer)
    scripts = [p for p in lock["local_deployed_files"] if p.endswith("/x.sh")]
    assert scripts == [".claude/hooks/hookpkg/hooks/scripts/x.sh"], lock
    world.apm(consumer, "audit", "--ci", "--no-policy")

    # Reinstalling the same bundle replaces its entries instead of duplicating them.
    world.apm(consumer, "install", str(bundle), "--no-policy")
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash"]}

    # A locally edited hook script survives a reinstall unless --force is passed.
    script = consumer / scripts[0]
    script.write_text("#!/bin/sh\necho edited\n")
    world.apm(consumer, "install", str(bundle), "--no-policy")
    assert script.read_text() == "#!/bin/sh\necho edited\n"
    world.apm(consumer, "install", str(bundle), "--no-policy", "--force")
    assert script.read_text() == "#!/bin/sh\necho hookpkg\n"

    # An unrelated dependency with hooks neither sweeps nor drifts bundle hooks.
    dep = _hooked_package(world.isolated.work_root / "otherpkg", "otherpkg", "Write", "os")
    _allow(consumer, key, f"{dep}#1.0.0")
    world.apm(consumer, "install", str(dep), "--no-policy")
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash", "Write"]}
    world.apm(consumer, "audit", "--ci", "--no-policy")
    world.apm(consumer, "uninstall", str(dep))
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash"]}
    assert _run_hook(world, consumer, "Bash") == "hookpkg\n"
    assert (consumer / ".claude" / "skills" / "hs" / "SKILL.md").is_file()
    world.apm(consumer, "audit", "--ci", "--no-policy")

    # A newer bundle whose hooks are not approved retires only this bundle's entries.
    world.apm(consumer, "install", str(dep), "--no-policy")
    unapproved, new_key = _repack(world, producer, tmp_path, "1.0.1", "#!/bin/sh\necho v2\n")
    text = output(world.apm(consumer, "install", str(unapproved), "--no-policy"))
    # #4216: the earlier hook is retired AND the new one is parked with its exact key.
    assert "Retired hooks from local bundle 'hookpkg'" in text, text
    assert "not approved for this exact bundle content" in text, text
    assert _hook_entries(consumer) == {"PreToolUse": ["Write"]}

    # Apply the printed remedy as printed: the catalog's apm.yml executables.allow
    # entry (#4506), then reinstall ('apm approve <key>' writes the same entry, #4437).
    printed = re.search(
        r"The same grant as this entry in apm\.yml: executables: allow: \"([^\"]+)\": (\w+): true",
        text,
    )
    assert printed is not None, text
    printed_key = re.sub(r"\s+", "", printed.group(1))
    assert printed_key == new_key, (printed_key, new_key)
    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"]["allow"][printed_key] = {printed.group(2): True}
    dump_yaml(manifest, consumer / "apm.yml")
    world.apm(consumer, "install", str(unapproved), "--no-policy")
    assert _hook_entries(consumer) == {"PreToolUse": ["Write", "Bash"]}
    assert _run_hook(world, consumer, "Bash") == "v2\n"
    _assert_lock_matches_disk(world, consumer)
    world.apm(consumer, "audit", "--ci", "--no-policy")

    world.apm(consumer, "install", str(bundle), "--no-policy")
    assert _hook_entries(consumer) == {"PreToolUse": ["Write", "Bash"]}

    # A newer bundle that ships no hooks retires them too; a dry run retires nothing.
    hookless, _ = _repack(world, producer, tmp_path, "1.0.2", None)
    settings = (consumer / ".claude" / "settings.json").read_bytes()
    world.apm(consumer, "install", str(hookless), "--no-policy", "--dry-run")
    assert (consumer / ".claude" / "settings.json").read_bytes() == settings
    world.apm(consumer, "install", str(hookless), "--no-policy")
    assert _hook_entries(consumer) == {"PreToolUse": ["Write"]}
    world.apm(consumer, "audit", "--ci", "--no-policy")


def test_bundle_hooks_register_for_copilot_and_user_scope(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    producer, bundle, key = _packed_bundle(world, tmp_path)

    # Copilot keeps one hook file per package; the bundle's matches a source install's.
    source = world.consumer("copilot-source", targets=("copilot",))
    world.apm(source, "install", str(producer), "--no-policy")
    world.apm(source, "approve", str(producer))
    world.apm(source, "install", "--no-policy")
    consumer = world.consumer("copilot-bundle", targets=("copilot",))
    _allow(consumer, key)
    world.apm(consumer, "install", str(bundle), "--no-policy")
    world.apm(consumer, "install", str(bundle), "--no-policy")

    def copilot_hooks(project: Path) -> dict[str, list[str]]:
        (hook_file,) = (project / ".github" / "hooks").glob("*.json")
        data = json.loads(hook_file.read_text())
        return {event: [e["matcher"] for e in entries] for event, entries in data["hooks"].items()}

    assert copilot_hooks(consumer) == copilot_hooks(source) == {"preToolUse": ["Bash"]}
    world.apm(consumer, "audit", "--ci", "--no-policy")

    # User scope registers once, with a cwd-independent command.
    home = world.isolated.home
    (home / ".claude").mkdir(exist_ok=True)
    write(home / ".apm" / "apm.yml", "name: user\nversion: 0.1.0\ntargets: [claude]\n")
    _allow(home / ".apm", key)
    world.apm(world.isolated.work_root, "install", "-g", str(bundle), "--no-policy")
    world.apm(world.isolated.work_root, "install", "-g", str(bundle), "--no-policy")
    assert _hook_entries(home) == {"PreToolUse": ["Bash"]}
    assert _run_hook(world, home, "Bash") == "hookpkg\n"

    # An approved newer bundle updates its user-scope script without --force.
    updated, updated_key = _repack(world, producer, tmp_path, "1.0.1", "#!/bin/sh\necho v2\n")
    _allow(home / ".apm", key, updated_key)
    world.apm(world.isolated.work_root, "install", "-g", str(updated), "--no-policy")
    assert _run_hook(world, home, "Bash") == "v2\n"

    # A newer user-scope bundle that ships no hooks retires them.
    hookless, _ = _repack(world, producer, tmp_path, "1.0.2", None)
    world.apm(world.isolated.work_root, "install", "-g", str(hookless), "--no-policy")
    assert _hook_entries(home) == {}


def test_approve_pending_lists_parked_bundle_executables(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4221: parked bundle hooks are pending, with the key the install printed."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = world.consumer("pending-consumer", targets=("claude",))
    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {}
    dump_yaml(manifest, consumer / "apm.yml")

    # #4492: an install that fails its integrity check records nothing pending.
    tampered = tmp_path / "tampered" / bundle.name
    shutil.copytree(bundle, tampered)
    (tampered / "hooks.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    before = tree_bytes(consumer)
    failed = world.apm(consumer, "install", str(tampered), expect=1)
    assert "Hash mismatch for hooks.json" in output(failed), output(failed)
    assert not (consumer / "apm_modules" / ".local-bundle-pending.json").exists()
    assert tree_bytes(consumer) == before

    installed = " ".join(output(world.apm(consumer, "install", str(bundle))).split())
    assert f'"{key}"' in installed, installed
    assert (consumer / "apm_modules" / ".local-bundle-pending.json").is_file()

    pending = " ".join(output(world.apm(consumer, "approve", "--pending")).split())
    assert "All packages with executables are approved" not in pending, pending
    assert "local bundle 'hookpkg'" in pending, pending
    assert key in pending, pending
    assert "hooks" in pending, pending

    # Approving the exact key and reinstalling deploys the hooks; nothing stays pending.
    _allow(consumer, key)
    world.apm(consumer, "install", str(bundle))
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash"]}
    pending = output(world.apm(consumer, "approve", "--pending"))
    assert "All packages with executables are approved." in pending, pending


def test_update_names_a_bundle_whose_executables_are_parked(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4441: 'apm update' names a parked bundle and its next step, and changes nothing."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = world.consumer("parked-update-consumer", targets=("claude",))
    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {}
    dump_yaml(manifest, consumer / "apm.yml")
    world.apm(consumer, "install", str(bundle))
    durable = ("apm.yml", "apm.lock.yaml", "apm_modules/.local-bundle-pending.json")
    before = {name: (consumer / name).read_bytes() for name in durable}

    updated = output(world.apm(consumer, "update", "--yes"))

    assert "Local bundle 'hookpkg': hooks parked, awaiting approval." in updated, updated
    assert "apm approve --pending" in updated, updated
    assert "executables.allow" in updated, updated
    assert {name: (consumer / name).read_bytes() for name in durable} == before

    # Once the exact key is approved and the bundle reinstalled, nothing is parked.
    _allow(consumer, key)
    world.apm(consumer, "install", str(bundle))
    assert "awaiting approval" not in output(world.apm(consumer, "update", "--yes"))


def _skill_bundle(world: MarketplacePackWorld, tmp_path: Path) -> Path:
    producer = world.isolated.work_root / "skillpkg"
    write(
        producer / "apm.yml",
        "name: skillpkg\nversion: 1.0.0\ndescription: s\nlicense: MIT\n"
        "targets: [copilot]\ndependencies:\n  apm: []\n",
    )
    write(producer / ".apm" / "skills" / "ss" / "SKILL.md", skill_md("ss"))
    world.apm(producer, "install", "--no-policy")
    world.apm(producer, "pack", "-o", str(tmp_path / "skill-out"))
    return tmp_path / "skill-out" / "skillpkg-1.0.0"


@pytest.mark.parametrize("edited", [False, True], ids=["unedited", "edited"])
def test_bundle_skill_migration_removes_receipted_legacy_copies(
    tmp_path: Path, apm_binary_path: Path, edited: bool
) -> None:
    """#4223: a bundle install records receipts, so skill path migration proves its files.

    A bundle cannot be rendered from a locked source; its receipts are the only
    deletion evidence (req-lk-024). An unedited legacy copy is removed when the
    converged layout is installed. An edited one is kept, named and refused
    with the #4360 exit and wording, and the converged copy still deploys;
    moving it aside, as the remedy says, lets the next install converge.
    """
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle = _skill_bundle(world, tmp_path)
    consumer = world.consumer("migrate", targets=("copilot",))
    install = ("install", str(bundle), "--target", "copilot", "--no-policy")
    world.apm(consumer, *install, "--legacy-skill-paths")
    legacy = consumer / ".github" / "skills" / "ss" / "SKILL.md"
    assert legacy.is_file()
    receipts = world.isolated.home / ".apm" / "deployment-receipts"
    assert receipts.is_dir() and any(receipts.iterdir()), "bundle install records receipts"
    if edited:
        legacy.write_text("# mine\n", encoding="ascii")

    result = world.apm(consumer, *install, expect=1 if edited else 0)

    assert (consumer / ".agents" / "skills" / "ss" / "SKILL.md").is_file()
    if edited:
        assert legacy.read_bytes() == b"# mine\n"
        output = " ".join((result.stdout + result.stderr).split())
        assert "Kept .github/skills/ss/SKILL.md:" in output, output
        assert "Move it aside, then run 'apm install'" in output, output
        # The remedy: move the edited copy aside, then the install converges.
        aside = tmp_path / "SKILL.md.mine"
        legacy.rename(aside)
        world.apm(consumer, *install)
        assert aside.read_bytes() == b"# mine\n"
    assert not legacy.exists()
    local = world.lock(consumer).get("local_deployed_files") or []
    assert not any(path.startswith(".github/skills/") for path in local), local


def _squeezed(result) -> str:
    """Combined output with all whitespace removed, so a wrapped key still matches."""
    return re.sub(r"\s+", "", result.stdout + result.stderr)


def _printed_key(text: str) -> str:
    """The exact-content key the install diagnostic printed, as the user copies it."""
    printed = re.search(r"executables: allow: \"([^\"]+)\": hooks: true", text)
    assert printed is not None, text
    return re.sub(r"\s+", "", printed.group(1))


def _approval_state(world: MarketplacePackWorld, project: Path) -> tuple[dict, bytes | None]:
    """Project bytes plus the user config store: what a refused approve must not touch."""
    config = Path(world.env["APM_HOME"]) / "config.json"
    return tree_bytes(project), config.read_bytes() if config.exists() else None


def _assert_refused(world: MarketplacePackWorld, project: Path, key: str) -> None:
    """I7/#4189: exit 1, an [x] line naming the key, and no project or user-store write."""
    before = _approval_state(world, project)
    refused = world.apm(project, "approve", key, expect=1)
    assert f"[x]{key}:noinstalledlocalbundlehasthisexactcontent" in _squeezed(refused)
    assert _approval_state(world, project) == before


def test_approve_grants_the_printed_local_bundle_key(tmp_path: Path, apm_binary_path: Path) -> None:
    """#4437: 'apm approve <printed key>' equals the executables.allow entry; stale keys fail."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = world.consumer("approve-key-consumer", targets=("claude",))
    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {}
    dump_yaml(manifest, consumer / "apm.yml")
    printed = _printed_key(output(world.apm(consumer, "install", str(bundle))))
    assert printed == key, (printed, key)
    assert not (consumer / ".claude" / "settings.json").exists()

    # A key with a wrong digest, or for a bundle that is not installed, is refused.
    digest = key.rsplit(":", 1)[1]
    _assert_refused(
        world, consumer, key[: -len(digest)] + ("0" if digest[0] != "0" else "1") * len(digest)
    )
    _assert_refused(world, consumer, "otherpkg" + key[len("hookpkg") :])

    # --pending prints a runnable approve command for the printed key; running it
    # writes the executables.allow entry, and the rerun install registers the hooks.
    pending = world.apm(consumer, "approve", "--pending")
    commands = re.findall(r"apm approve '([^']+)'", pending.stdout)
    assert commands == [key], pending.stdout
    approved = output(world.apm(consumer, "approve", commands[0]))
    assert "[+] Approved local bundle 'hookpkg': hooks" in approved, approved
    assert load_yaml(consumer / "apm.yml")["executables"] == {"allow": {key: {"hooks": True}}}
    world.apm(consumer, "install", str(bundle))
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash"]}
    assert _run_hook(world, consumer, "Bash") == "hookpkg\n"
    _assert_lock_matches_disk(world, consumer)
    pending = output(world.apm(consumer, "approve", "--pending"))
    assert "All packages with executables are approved." in pending, pending
    # Approving the installed key again converges: no byte changes.
    before = tree_bytes(consumer)
    world.apm(consumer, "approve", printed)
    assert tree_bytes(consumer) == before

    # Changed bundle bytes park again under a new key; the old key is now stale.
    changed, new_key = _repack(world, producer, tmp_path, "1.0.1", "#!/bin/sh\necho v2\n")
    assert _printed_key(output(world.apm(consumer, "install", str(changed)))) == new_key
    assert "Bash" not in json.dumps(_hook_entries(consumer)), "the stale grant ran new bytes"
    _assert_refused(world, consumer, key)

    world.apm(consumer, "approve", new_key)
    world.apm(consumer, "install", str(changed))
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash"]}
    assert _run_hook(world, consumer, "Bash") == "v2\n"


def test_tampered_bundle_refusal_names_the_reason_and_remedy(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """RA-038: an integrity refusal names its reason and catalog remedy, never an empty error."""
    from apm_cli.core.remediation import BUNDLE_INTEGRITY_FAILED

    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _, bundle, _ = _packed_bundle(world, tmp_path)
    tampered = tmp_path / "tampered"
    shutil.copytree(bundle, tampered)
    (tampered / "skills" / "hs" / "SKILL.md").write_text("# altered\n", encoding="utf-8")
    consumer = world.consumer("tamper-consumer", targets=("claude",))
    before = tree_bytes(consumer)

    result = world.apm(consumer, "install", str(tampered), "--no-policy", expect=1)
    # One refusal, one stream: headline, reason and remedy all on stderr.
    stderr = " ".join(result.stderr.split())
    refusal = (
        "Bundle integrity check failed:",
        "Hash mismatch for skills/hs/SKILL.md",
        BUNDLE_INTEGRITY_FAILED.sentence(),
    )
    for part in refusal:
        assert part in stderr, result.stderr
        assert part not in result.stdout, result.stdout
    assert "Error installing dependencies" not in result.stdout + result.stderr
    assert tree_bytes(consumer) == before


# -- the bundle's hook sidecar is in the config file records (#5039) -------------------

_SIDECAR = ".github/apm-hooks.json"


def _named_bundle(world: MarketplacePackWorld, tmp_path: Path, name: str) -> tuple[Path, str]:
    producer = _hooked_package(world.isolated.work_root / name, name, "Bash", f"{name}-skill")
    world.apm(producer, "install", "--no-policy")
    world.apm(producer, "pack", "-o", str(tmp_path / f"out-{name}"))
    bundle = tmp_path / f"out-{name}" / f"{name}-1.0.0"
    info = detect_local_bundle(bundle)
    return bundle, local_bundle_approval_key(
        info.package_id, str(info.plugin_json.get("version") or ""), info.source_dir, info.lockfile
    )


def _recorded(world: MarketplacePackWorld, path: Path) -> dict | None:
    """This machine's config file record of *path* (what crash recovery verifies)."""
    records = Path(world.env["APM_HOME"]) / "deployment-receipts" / "config-files"
    rows = [
        row
        for store in (sorted(records.glob("*.json")) if records.is_dir() else ())
        for row in json.loads(store.read_text(encoding="utf-8")).get("files", [])
    ]
    return next((row for row in rows if row["path"] == path.resolve().as_posix()), None)


def test_bundle_sidecar_writes_keep_the_config_file_records_current(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Every write of the bundle hook sidecar is recorded with the bytes it left, so
    crash recovery can verify the sidecar when a journaled install later changes it."""
    import hashlib

    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle_a, key_a = _named_bundle(world, tmp_path, "pkga")
    bundle_b, key_b = _named_bundle(world, tmp_path, "pkgb")
    consumer = world.consumer("bundle-consumer", targets=("copilot",))
    _allow(consumer, key_a, key_b)
    sidecar = consumer / _SIDECAR

    for bundle in (bundle_a, bundle_b):
        world.apm(consumer, "install", str(bundle), "--no-policy")
        row = _recorded(world, sidecar)
        assert row is not None, f"no record of {_SIDECAR} after installing {bundle.name}"
        digest = "sha256:" + hashlib.sha256(sidecar.read_bytes()).hexdigest()
        assert row["hash"] == digest, (row, sidecar.read_text(encoding="utf-8"))


@pytest.mark.parametrize("outside", ["dangling", "json"])
def test_bundle_sidecar_link_is_replaced_and_its_target_left_alone(
    tmp_path: Path, apm_binary_path: Path, outside: str
) -> None:
    """A linked hook record file is replaced by a regular file; what it linked to is untouched."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle, key = _named_bundle(world, tmp_path, "pkga")
    consumer = world.consumer("linked-sidecar", targets=("copilot",))
    _allow(consumer, key)
    target = tmp_path / "outside" / "hooks.json"
    target.parent.mkdir()
    if outside == "json":
        target.write_text('{"keep": true}\n', encoding="utf-8")
    before = target.read_bytes() if target.exists() else None
    sidecar = consumer / _SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    try:
        sidecar.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    world.apm(consumer, "install", str(bundle), "--no-policy")

    assert (target.read_bytes() if target.exists() else None) == before
    assert sidecar.is_file() and not sidecar.is_symlink()


def _bundle_hook_claims(world: MarketplacePackWorld, project: Path) -> list[str]:
    lock = world.lock(project)
    return [p for p in lock.get("local_deployed_files") or [] if "/hooks/" in p]


def test_uninstall_bundle_removes_its_hooks_and_claims(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3700: 'apm uninstall <bundle>' retires what 'apm install <bundle>' registered.

    Hooks park until approved, register with lock claims, and leave with
    'apm uninstall' through the hook owner: the bundle's merged entries, its
    scripts and their claims go; user and sibling entries and the bundle's
    non-hook files stay, and the kept files are named.
    """
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = world.consumer("uninstall-consumer", targets=("claude",))
    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {}
    dump_yaml(manifest, consumer / "apm.yml")
    user_entry = {"matcher": "Mine", "hooks": [{"type": "command", "command": "true"}]}
    write(consumer / ".claude" / "settings.json", json.dumps({"hooks": {"Stop": [user_entry]}}))

    # Parked until approved: no entry, no script, no claim.
    parked = output(world.apm(consumer, "install", str(bundle)))
    assert "Skipped 2 bundle hook file(s) from hookpkg" in parked, parked
    assert _hook_entries(consumer) == {"Stop": ["Mine"]}
    assert _bundle_hook_claims(world, consumer) == []

    # Approved: registered with a lock claim, next to an unrelated hooked dependency.
    world.apm(consumer, "approve", key)
    world.apm(consumer, "install", str(bundle))
    dep = _hooked_package(world.isolated.work_root / "otherpkg", "otherpkg", "Write", "os")
    _allow(consumer, key, f"{dep}#1.0.0")
    world.apm(consumer, "install", str(dep))
    assert _hook_entries(consumer) == {"Stop": ["Mine"], "PreToolUse": ["Bash", "Write"]}
    script = ".claude/hooks/hookpkg/hooks/scripts/x.sh"
    assert _bundle_hook_claims(world, consumer) == [script]
    _assert_lock_matches_disk(world, consumer)

    # A dry run names the removal and writes nothing.
    before = tree_bytes(consumer)
    preview = output(world.apm(consumer, "uninstall", str(bundle), "--dry-run"))
    assert "Would remove hooks from local bundle 'hookpkg'" in preview, preview
    assert tree_bytes(consumer) == before

    # Mixing a bundle with an apm.yml package is refused before any write.
    mixed = output(world.apm(consumer, "uninstall", str(bundle), str(dep), expect=1))
    assert f"cannot be uninstalled with other packages ({dep})" in mixed, mixed
    assert "Run 'apm uninstall <bundle>' to remove a local bundle's hooks" in mixed, mixed
    assert tree_bytes(consumer) == before

    removed = world.apm(consumer, "uninstall", str(bundle))
    text = output(removed)
    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    assert "Kept 1 other file(s) from local bundles" in text, text
    assert "cannot remove local-bundle content" not in text, text
    assert removed.stdout.isascii() and removed.stderr.isascii()
    after = tree_bytes(consumer)
    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    assert changed == {
        ".claude/settings.json",
        ".claude/apm-hooks.json",
        "apm.lock.yaml",
        "apm_modules/.local-bundle-pending.json",
        script,
    }, changed
    assert _hook_entries(consumer) == {"Stop": ["Mine"], "PreToolUse": ["Write"]}
    assert "local-bundle::hookpkg" not in (consumer / ".claude" / "apm-hooks.json").read_text()
    assert _bundle_hook_claims(world, consumer) == []
    assert not (consumer / ".claude" / "hooks" / "hookpkg").exists()
    assert (consumer / ".claude" / "skills" / "hs" / "SKILL.md").is_file()
    assert ".claude/skills/hs/SKILL.md" in world.lock(consumer)["local_deployed_files"]
    world.apm(consumer, "audit", "--ci")
    pending = output(world.apm(consumer, "approve", "--pending"))
    assert "All packages with executables are approved." in pending, pending

    # Converged: a second uninstall finds nothing to remove and changes no bytes.
    again = output(world.apm(consumer, "uninstall", str(bundle), expect=1))
    assert "Local bundle 'hookpkg' has no hooks deployed" in again, again
    assert tree_bytes(consumer) == after

    # Symmetric: reinstalling the approved bundle registers it again.
    world.apm(consumer, "install", str(bundle))
    assert _hook_entries(consumer) == {"Stop": ["Mine"], "PreToolUse": ["Write", "Bash"]}
    assert _bundle_hook_claims(world, consumer) == [script]

    # User scope, selected by the bundle name once the bundle directory is gone.
    home = world.isolated.home
    (home / ".claude").mkdir(exist_ok=True)
    write(home / ".apm" / "apm.yml", "name: user\nversion: 0.1.0\ntargets: [claude]\n")
    _allow(home / ".apm", key)
    world.apm(world.isolated.work_root, "install", "-g", str(bundle))
    assert _hook_entries(home) == {"PreToolUse": ["Bash"]}
    shutil.rmtree(bundle)
    text = output(world.apm(world.isolated.work_root, "uninstall", "-g", "hookpkg"))
    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    assert _hook_entries(home) == {}
    assert not (home / ".claude" / "hooks" / "hookpkg").exists()
    user_lock = load_yaml(home / ".apm" / "apm.lock.yaml")
    assert not [p for p in user_lock.get("local_deployed_files") or [] if "/hooks/" in p]


def _approved_consumer(world: MarketplacePackWorld, name: str, key: str) -> Path:
    consumer = world.consumer(name, targets=("claude",))
    _allow(consumer, key)
    return consumer


def _copilot_hook_files(project: Path) -> list[str]:
    hooks = project / ".github" / "hooks"
    return sorted(p.name for p in hooks.glob("*.json")) if hooks.is_dir() else []


def test_uninstall_bundle_reaches_targets_outside_the_manifest(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3700 review: hooks deployed with an explicit --target leave with the bundle."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "override-consumer", key)
    (consumer / ".github").mkdir()
    world.apm(consumer, "install", str(bundle), "--target", "claude,copilot")
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash"]}
    assert _copilot_hook_files(consumer), "copilot hooks were not deployed"

    text = output(world.apm(consumer, "uninstall", str(bundle)))
    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    assert _hook_entries(consumer) == {}
    assert _copilot_hook_files(consumer) == []
    assert _bundle_hook_claims(world, consumer) == []

    # A copilot-only deployment is found too, not refused as missing.
    world.apm(consumer, "install", str(bundle), "--target", "copilot")
    assert _copilot_hook_files(consumer)
    world.apm(consumer, "uninstall", str(bundle))
    assert _copilot_hook_files(consumer) == []


def test_uninstall_bundle_keeps_ownership_of_a_hook_it_could_not_remove(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3700 review: a hook file replaced by a symlink fails the uninstall and stays owned."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = world.consumer("symlink-consumer", targets=("copilot",))
    _allow(consumer, key)
    world.apm(consumer, "install", str(bundle))
    (hook_name,) = _copilot_hook_files(consumer)
    hook = consumer / ".github" / "hooks" / hook_name
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(hook.read_bytes())
    hook.unlink()
    hook.symlink_to(elsewhere)
    sidecar = (consumer / ".github" / "apm-hooks.json").read_text()

    result = world.apm(consumer, "uninstall", str(bundle), expect=1)
    text = output(result)
    assert "Removed hooks from local bundle" not in text, text
    assert "could not all be removed" in text, text
    assert f".github/hooks/{hook_name}" in text, text
    assert hook.is_symlink() and elsewhere.is_file()
    assert "local-bundle::hookpkg" in (consumer / ".github" / "apm-hooks.json").read_text()
    assert sidecar.count("local-bundle::hookpkg") == (
        (consumer / ".github" / "apm-hooks.json").read_text().count("local-bundle::hookpkg")
    )
    claims = world.lock(consumer)["local_deployed_files"]
    assert f".github/hooks/{hook_name}" in claims
    # The retained hook still runs its scripts, so they and their claims stay too.
    scripts = [claim for claim in claims if claim.endswith("/x.sh")]
    assert scripts, claims
    assert all((consumer / claim).is_file() for claim in scripts), scripts
    assert (consumer / "apm_modules" / ".local-bundle-pending.json").is_file()


def test_uninstall_bundle_path_selects_its_installed_alias(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3700 review: a bundle installed with --as is uninstalled by its path; twins are refused."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "alias-consumer", key)
    world.apm(consumer, "install", str(bundle), "--as", "team-hooks")
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash"]}

    text = output(world.apm(consumer, "uninstall", str(bundle)))
    assert "Removed hooks from local bundle 'team-hooks'" in text, text
    assert _hook_entries(consumer) == {}

    # The same bundle installed twice (default name and alias) is ambiguous by path.
    world.apm(consumer, "install", str(bundle))
    world.apm(consumer, "install", str(bundle), "--as", "team-hooks")
    before = tree_bytes(consumer)
    refused = output(world.apm(consumer, "uninstall", str(bundle), expect=1))
    assert "matches 2 installed local bundles (hookpkg, team-hooks)" in refused, refused
    assert tree_bytes(consumer) == before
    # The printed remedy: uninstall one by its installed name.
    world.apm(consumer, "uninstall", "team-hooks")
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash"]}


def test_uninstall_bundle_path_without_dot_slash(tmp_path: Path, apm_binary_path: Path) -> None:
    """#3700 review: uninstall accepts the bundle path spellings install accepts."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "relpath-consumer", key)
    local = consumer / "build" / bundle.name
    shutil.copytree(bundle, local)
    rel = f"build/{bundle.name}"
    world.apm(consumer, "install", rel)
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash"]}

    text = output(world.apm(consumer, "uninstall", rel))
    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    assert _hook_entries(consumer) == {}


def test_uninstall_bundle_refuses_a_linked_hook_config_root(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3700 review: a symlinked root of a target outside the manifest is refused, not rewritten."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "linked-root-consumer", key)
    (consumer / ".gemini").mkdir()
    world.apm(consumer, "install", str(bundle), "--target", "claude,gemini")
    assert "local-bundle::hookpkg" in (consumer / ".gemini" / "apm-hooks.json").read_text()
    outside = tmp_path / "outside-gemini"
    shutil.move(str(consumer / ".gemini"), outside)
    (consumer / ".gemini").symlink_to(outside, target_is_directory=True)
    before, outside_before = tree_bytes(consumer), tree_bytes(outside)

    refused = output(world.apm(consumer, "uninstall", str(bundle), expect=1))
    assert "Local bundle hooks were not removed: .gemini/settings.json (linked path)" in refused
    assert "no changes were made" in refused, refused
    assert tree_bytes(consumer) == before
    assert tree_bytes(outside) == outside_before


def test_uninstall_bundle_refuses_a_symlinked_lockfile_before_removing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3700 review: a lockfile APM cannot rewrite stops the uninstall before any removal."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "linked-lock-consumer", key)
    world.apm(consumer, "install", str(bundle))
    real_lock = tmp_path / "real-apm.lock.yaml"
    shutil.move(str(consumer / "apm.lock.yaml"), real_lock)
    (consumer / "apm.lock.yaml").symlink_to(real_lock)
    before, lock_bytes = tree_bytes(consumer), real_lock.read_bytes()

    refused = world.apm(consumer, "uninstall", str(bundle), expect=1)
    assert "symbolic link" in output(refused), output(refused)
    assert tree_bytes(consumer) == before
    assert real_lock.read_bytes() == lock_bytes
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash"]}


def test_uninstall_bundle_refuses_when_a_hook_config_is_unreadable(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3700 review: an unreadable hook config on any target cannot confirm removal."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "unreadable-consumer", key)
    (consumer / ".gemini").mkdir()
    world.apm(consumer, "install", str(bundle), "--target", "claude,gemini")
    write(consumer / ".gemini" / "apm-hooks.json", "{not json")
    before = tree_bytes(consumer)

    refused = output(world.apm(consumer, "uninstall", str(bundle), expect=1))
    assert ".gemini/apm-hooks.json (unreadable)" in refused, refused
    assert "Removed hooks" not in refused, refused
    assert tree_bytes(consumer) == before
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash"]}


def test_uninstall_bundle_leaves_an_unrelated_sidecar_byte_identical(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3700 review: a hook sidecar with no record for the bundle is never rewritten."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "unrelated-sidecar-consumer", key)
    (consumer / ".github").mkdir()
    sidecar = write(consumer / ".github" / "apm-hooks.json", '{"local-bundle-hooks":[]}')
    world.apm(consumer, "install", str(bundle), "--target", "claude")

    world.apm(consumer, "uninstall", str(bundle))
    assert _hook_entries(consumer) == {}
    assert sidecar.read_bytes() == b'{"local-bundle-hooks":[]}'


def test_uninstall_bundle_refuses_a_linked_pending_record(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3700 review: a symlinked pending record is refused before any hook is removed."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "linked-pending-consumer", key)
    world.apm(consumer, "install", str(bundle))
    world.apm(consumer, "install", str(bundle), "--as", "team-hooks")
    record = consumer / "apm_modules" / ".local-bundle-pending.json"
    outside = tmp_path / "outside-pending.json"
    shutil.move(str(record), outside)
    record.symlink_to(outside)
    before, outside_bytes = tree_bytes(consumer), outside.read_bytes()

    refused = output(world.apm(consumer, "uninstall", "hookpkg", expect=1))
    assert "apm_modules/.local-bundle-pending.json (linked path)" in refused, refused
    assert tree_bytes(consumer) == before
    assert outside.read_bytes() == outside_bytes


def test_uninstall_user_bundle_ignores_an_unrelated_invalid_user_root(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3700 review: a relative COPILOT_HOME does not block a Claude-only user-scope uninstall."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    home = world.isolated.home
    (home / ".claude").mkdir(exist_ok=True)
    write(home / ".apm" / "apm.yml", "name: user\nversion: 0.1.0\ntargets: [claude]\n")
    _allow(home / ".apm", key)
    world.apm(world.isolated.work_root, "install", "-g", str(bundle))
    assert _hook_entries(home) == {"PreToolUse": ["Bash"]}

    world.env = {**world.env, "COPILOT_HOME": "relative-copilot-home"}
    text = output(world.apm(world.isolated.work_root, "uninstall", "-g", "hookpkg"))
    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    assert _hook_entries(home) == {}


def _claims(world: MarketplacePackWorld, project: Path) -> list[str]:
    return world.lock(project).get("local_deployed_files") or []


def test_uninstall_bundle_named_scripts_keeps_sibling_copilot_scripts(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3700 review: a bundle named 'scripts' matches only its own per-file script layout."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = world.consumer("scripts-alias-consumer", targets=("copilot",))
    _allow(consumer, key)
    world.apm(consumer, "install", str(bundle))
    world.apm(consumer, "install", str(bundle), "--as", "scripts")
    sibling = [
        c for c in _claims(world, consumer) if c.startswith(".github/hooks/scripts/hookpkg/")
    ]
    assert sibling, _claims(world, consumer)

    world.apm(consumer, "uninstall", "scripts")
    assert _copilot_hook_files(consumer) == ["hookpkg-hooks.json"]
    claims = _claims(world, consumer)
    assert all(c in claims and (consumer / c).is_file() for c in sibling), claims
    assert not [c for c in claims if c.startswith(".github/hooks/scripts/scripts/")], claims


def test_uninstall_bundle_path_with_the_gate_off_never_guesses_an_alias(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3700 review: with no recorded content identity, a path that could name an alias is refused."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, _key = _packed_bundle(world, tmp_path)
    consumer = world.consumer("gate-off-consumer", targets=("claude",))
    world.apm(consumer, "install", str(bundle))
    world.apm(consumer, "install", str(bundle), "--as", "team-hooks")
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash", "Bash"]}
    before = tree_bytes(consumer)

    refused = output(world.apm(consumer, "uninstall", str(bundle), expect=1))
    assert "hookpkg, team-hooks recorded no content identity" in refused, refused
    assert "Run 'apm uninstall <installed-name>'" in refused, refused
    assert tree_bytes(consumer) == before

    # The printed remedy, by installed name, removes exactly that copy.
    world.apm(consumer, "uninstall", "team-hooks")
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash"]}
    # A single default-named copy is still selected by its path.
    world.apm(consumer, "uninstall", str(bundle))
    assert _hook_entries(consumer) == {}


def test_uninstall_bundle_reaches_a_partially_retired_bundle(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3700 review: sidecar markers and script claims left after native entries go stay reachable."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "partial-consumer", key)
    world.apm(consumer, "install", str(bundle))
    # A retirement that rewrote settings.json but not its sidecar.
    write(consumer / ".claude" / "settings.json", "{}\n")
    script = ".claude/hooks/hookpkg/hooks/scripts/x.sh"
    assert script in _claims(world, consumer)

    text = output(world.apm(consumer, "uninstall", str(bundle)))
    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    sidecar = consumer / ".claude" / "apm-hooks.json"
    assert not sidecar.exists() or "local-bundle::hookpkg" not in sidecar.read_text()
    assert not (consumer / script).exists()
    assert script not in _claims(world, consumer)


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs a non-root user")
def test_uninstall_bundle_retains_a_hook_file_it_cannot_delete(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3700 review: a hook file whose removal fails is retained and named, not a crash."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = world.consumer("locked-consumer", targets=("copilot",))
    _allow(consumer, key)
    world.apm(consumer, "install", str(bundle))
    hooks_dir = consumer / ".github" / "hooks"
    hooks_dir.chmod(0o555)
    try:
        result = world.apm(consumer, "uninstall", str(bundle), expect=1)
    finally:
        hooks_dir.chmod(0o755)
    text = output(result)
    assert "could not all be removed (.github/hooks/hookpkg-hooks.json" in text, text
    assert "Traceback" not in text and "Error uninstalling" not in text, text
    claims = _claims(world, consumer)
    assert ".github/hooks/hookpkg-hooks.json" in claims
    assert "local-bundle::hookpkg" in (consumer / ".github" / "apm-hooks.json").read_text()
    scripts = [c for c in claims if c.endswith("/x.sh")]
    assert scripts and all((consumer / c).is_file() for c in scripts), claims


def test_bundle_hook_retirement_keeps_a_script_it_cannot_delete(
    tmp_path: Path, apm_binary_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3700 review: an undeletable script keeps its claim and fails the retirement closed.

    The trust re-check raises instead of reporting success, and a reinstall's
    retirement records an error instead of claiming the hooks were retired.
    """
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.local_bundle_hook_trust import (
        BundleHookRetirementIncomplete,
        retire_unadmitted_bundle_hooks,
    )
    from apm_cli.install.local_bundle_hooks import _retire_bundle_hooks
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.utils.diagnostics import DiagnosticCollector

    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "undeletable-script-consumer", key)
    world.apm(consumer, "install", str(bundle))
    script = ".claude/hooks/hookpkg/hooks/scripts/x.sh"
    assert script in _claims(world, consumer)
    real_unlink = Path.unlink

    def unlink(self: Path, *args, **kwargs) -> None:
        if self.name == "x.sh":
            raise PermissionError(13, "Permission denied", str(self))
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    targets = [KNOWN_TARGETS["claude"]]

    with pytest.raises(BundleHookRetirementIncomplete, match=r"hookpkg: .*x\.sh"):
        retire_unadmitted_bundle_hooks(consumer, targets, dict, lock_dir=consumer)
    assert _hook_entries(consumer) == {}
    assert (consumer / script).is_file()
    assert script in _claims(world, consumer)

    diagnostics = DiagnosticCollector()
    retired = _retire_bundle_hooks(
        consumer, targets, "hookpkg", scope=InstallScope.PROJECT, diagnostics=diagnostics
    )
    assert retired is False
    assert diagnostics.error_count == 1
    assert (consumer / script).is_file()
    assert script in _claims(world, consumer)


def _copilot_bundle_consumer(world: MarketplacePackWorld, tmp_path: Path, name: str):
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = world.consumer(name, targets=("copilot",))
    _allow(consumer, key)
    world.apm(consumer, "install", str(bundle))
    (hook_name,) = _copilot_hook_files(consumer)
    return consumer, f".github/hooks/{hook_name}"


def test_bundle_hook_retirement_reports_a_sidecar_it_cannot_rewrite(
    tmp_path: Path, apm_binary_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4679 review: a failed sidecar write still reconciles deleted hooks and fails closed."""
    from apm_cli.install.local_bundle_hook_trust import retire_bundle_hooks
    from apm_cli.integration.targets import KNOWN_TARGETS

    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    consumer, hook = _copilot_bundle_consumer(world, tmp_path, "sidecar-write-consumer")
    scripts = [c for c in _claims(world, consumer) if c.endswith("/x.sh")]
    assert scripts

    real_write, real_unlink = Path.write_text, Path.unlink

    def guarded(real):
        def call(self: Path, *args, **kwargs):
            if self.name == "apm-hooks.json":
                raise PermissionError(13, "Permission denied", str(self))
            return real(self, *args, **kwargs)

        return call

    monkeypatch.setattr(Path, "write_text", guarded(real_write))
    monkeypatch.setattr(Path, "unlink", guarded(real_unlink))
    retired = retire_bundle_hooks(
        consumer, [KNOWN_TARGETS["copilot"]], "hookpkg", lock_dir=consumer, reason="revoked"
    )
    assert ".github/apm-hooks.json" in retired.failed_paths, retired
    assert not (consumer / hook).exists()
    claims = _claims(world, consumer)
    assert hook not in claims
    assert all(c in claims and (consumer / c).is_file() for c in scripts), claims


def test_bundle_hook_retirement_fails_closed_on_a_kept_hook_config(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4679 review: a symlinked per-file hook is a failed retirement, never announced as done."""
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.local_bundle_hook_trust import (
        BundleHookRetirementIncomplete,
        retire_unadmitted_bundle_hooks,
    )
    from apm_cli.install.local_bundle_hooks import _retire_bundle_hooks
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.utils.diagnostics import DiagnosticCollector

    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    consumer, hook = _copilot_bundle_consumer(world, tmp_path, "kept-config-consumer")
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes((consumer / hook).read_bytes())
    (consumer / hook).unlink()
    (consumer / hook).symlink_to(elsewhere)
    targets = [KNOWN_TARGETS["copilot"]]

    with pytest.raises(BundleHookRetirementIncomplete, match=re.escape(hook)):
        retire_unadmitted_bundle_hooks(consumer, targets, dict, lock_dir=consumer)
    diagnostics = DiagnosticCollector()
    retired = _retire_bundle_hooks(
        consumer, targets, "hookpkg", scope=InstallScope.PROJECT, diagnostics=diagnostics
    )
    assert retired is False
    assert diagnostics.error_count == 1
    assert (consumer / hook).is_symlink()
    assert hook in _claims(world, consumer)


def test_uninstall_user_bundle_with_a_home_rooted_claude_config(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4679 review: CLAUDE_CONFIG_DIR=$HOME roots Claude at '.', and its scripts still leave."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    home = world.isolated.home
    world.env["CLAUDE_CONFIG_DIR"] = str(home)
    write(home / ".apm" / "apm.yml", "name: user\nversion: 0.1.0\ntargets: [claude]\n")
    _allow(home / ".apm", key)
    world.apm(world.isolated.work_root, "install", "-g", str(bundle))
    lock_path = home / ".apm" / "apm.lock.yaml"
    scripts = [
        p for p in load_yaml(lock_path).get("local_deployed_files") or [] if p.endswith("/x.sh")
    ]
    assert scripts, load_yaml(lock_path)

    text = output(world.apm(world.isolated.work_root, "uninstall", "-g", "hookpkg"))
    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    claims = load_yaml(lock_path).get("local_deployed_files") or []
    assert not [p for p in claims if p.endswith("/x.sh")], claims
    assert not (home / "hooks" / "hookpkg").exists()


# The pending record is locked while uninstall drops the bundle's hooks entry:
# the OS refuses that one update (rewrite or delete); every other write runs.
_LOCKED_PENDING_RECORD = """
import errno
import json
import os
from pathlib import Path

from apm_cli.cli import cli

_RECORD = ".local-bundle-pending.json"
_write_text, _unlink, _replace = Path.write_text, Path.unlink, os.replace
_SLUG = os.environ.get("APM_TEST_LOCK_PENDING_RECORD")


def _drops_hooks(data):
    # The update that drops the locked bundle's hooks entry (any bundle when "1").
    if _SLUG == "1":
        return '"hooks"' not in data
    entry = json.loads(data).get("bundles", {}).get(_SLUG) or {}
    return "hooks" not in entry.get("exec_types", [])


def _locked(path):
    return bool(_SLUG) and path.name == _RECORD


def write_text(self, data, *args, **kwargs):
    if _locked(self) and _drops_hooks(data):
        raise PermissionError(errno.EACCES, "injected locked pending record", str(self))
    return _write_text(self, data, *args, **kwargs)


def unlink(self, *args, **kwargs):
    if _locked(self):
        raise PermissionError(errno.EACCES, "injected locked pending record", str(self))
    return _unlink(self, *args, **kwargs)


def replace(src, dst, *args, **kwargs):
    if _locked(Path(dst)) and _drops_hooks(Path(src).read_text(encoding="utf-8")):
        raise PermissionError(errno.EACCES, "injected locked pending record", str(dst))
    return _replace(src, dst, *args, **kwargs)


Path.write_text = write_text
Path.unlink = unlink
os.replace = replace
cli()
"""


def _source_apm(world: MarketplacePackWorld, cwd: Path, *args: str, expect: int, locked: str = "1"):
    """Run the source CLI with the pending record locked (an OS boundary fault).

    *locked* names the bundle whose hooks-entry update fails ("1": any bundle).
    """
    import sys

    from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner

    env = dict(world.env)
    env["APM_TEST_LOCK_PENDING_RECORD"] = locked
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in (env.get("PYTHONPATH"), *(str(Path(x).resolve()) for x in sys.path if x)) if p]
    )
    runner = ApmLifecycleRunner((sys.executable, "-c", _LOCKED_PENDING_RECORD))
    result = runner.run(args, scenario_id="locked-pending", cwd=cwd, env=env)
    assert result.returncode == expect, (result.stdout, result.stderr)
    return result


def _pending_types(project: Path) -> dict[str, list[str]]:
    record = project / "apm_modules" / ".local-bundle-pending.json"
    if not record.is_file():
        return {}
    bundles = json.loads(record.read_text(encoding="utf-8"))["bundles"]
    return {slug: entry["exec_types"] for slug, entry in bundles.items()}


def test_uninstall_bundle_finishes_a_pending_record_update_that_failed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4987: a retry after a failed pending-record update clears the stale hooks entry.

    The hooks, scripts and claims are removed, then the pending-record update
    fails (the file is locked). Once access is fixed, a retry finishes that
    update and changes no other file.
    """
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "locked-pending-consumer", key)
    world.apm(consumer, "install", str(bundle))
    assert "hooks" in _pending_types(consumer)["hookpkg"]
    script = ".claude/hooks/hookpkg/hooks/scripts/x.sh"
    assert _bundle_hook_claims(world, consumer) == [script]

    failed = _source_apm(world, consumer, "uninstall", "hookpkg", expect=1)
    text = " ".join(output(failed).split())
    assert "Traceback" not in failed.stderr, failed.stderr
    assert "apm_modules/.local-bundle-pending.json" in text, text
    assert "run 'apm uninstall hookpkg' again" in text, text
    assert _hook_entries(consumer) == {}
    assert _bundle_hook_claims(world, consumer) == []
    assert "hooks" in _pending_types(consumer)["hookpkg"], "the stale entry is the fault's"

    before = tree_bytes(consumer)
    retried = world.apm(consumer, "uninstall", "hookpkg")
    assert "Removed hooks from local bundle 'hookpkg'" in output(retried), output(retried)
    after = tree_bytes(consumer)
    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    assert changed == {"apm_modules/.local-bundle-pending.json"}, changed
    assert "hooks" not in _pending_types(consumer).get("hookpkg", [])
    world.apm(consumer, "audit", "--ci")
    pending = output(world.apm(consumer, "approve", "--pending"))
    assert "All packages with executables are approved." in pending, pending
    # Converged: nothing is left to finish, and a repeat changes no bytes.
    again = output(world.apm(consumer, "uninstall", "hookpkg", expect=1))
    assert "no changes were made" in again, again
    assert tree_bytes(consumer) == after


def test_uninstall_refuses_a_bundle_whose_hooks_are_only_parked(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4987 control: a parked-only bundle (no failed cleanup) is still refused as today."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, _key = _packed_bundle(world, tmp_path)
    consumer = world.consumer("parked-only-consumer", targets=("claude",))
    manifest = load_yaml(consumer / "apm.yml")
    manifest["executables"] = {}
    dump_yaml(manifest, consumer / "apm.yml")
    parked = output(world.apm(consumer, "install", str(bundle)))
    assert "Skipped 2 bundle hook file(s) from hookpkg" in parked, parked
    assert "hooks" in _pending_types(consumer)["hookpkg"]
    before = tree_bytes(consumer)

    refused = output(world.apm(consumer, "uninstall", "hookpkg", expect=1))

    assert "Local bundle 'hookpkg' has no hooks deployed" in refused, refused
    assert tree_bytes(consumer) == before


def test_uninstall_bundle_refuses_an_unreadable_pending_record(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4992 review: an unreadable pending record stops uninstall before any hook is removed."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "unreadable-pending-consumer", key)
    world.apm(consumer, "install", str(bundle))
    record = consumer / "apm_modules" / ".local-bundle-pending.json"
    record.write_text("{not json\n", encoding="utf-8")
    before = tree_bytes(consumer)

    refused = " ".join(output(world.apm(consumer, "uninstall", str(bundle), expect=1)).split())

    assert "apm_modules/.local-bundle-pending.json cannot be read" in refused, refused
    assert "no hooks were removed" in refused, refused
    assert tree_bytes(consumer) == before


def test_uninstall_reports_a_kept_hook_when_another_bundle_record_update_fails(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4992 review: one bundle keeps an undeletable hook while another's record
    update fails; both failures are reported, each with its recovery step."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = world.consumer("two-failures-consumer", targets=("copilot",))
    _allow(consumer, key)
    world.apm(consumer, "install", str(bundle))
    (kept_name,) = _copilot_hook_files(consumer)
    world.apm(consumer, "install", str(bundle), "--as", "team-hooks")
    hook = consumer / ".github" / "hooks" / kept_name
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(hook.read_bytes())
    hook.unlink()
    hook.symlink_to(elsewhere)

    failed = _source_apm(
        world, consumer, "uninstall", "hookpkg", "team-hooks", expect=1, locked="team-hooks"
    )

    text = " ".join(output(failed).split())
    assert "Traceback" not in failed.stderr, failed.stderr
    assert "Hooks from local bundle 'hookpkg' could not all be removed" in text, text
    assert f".github/hooks/{kept_name}" in text, text
    assert "Removed hooks from local bundle 'team-hooks', but" in text, text
    assert "run 'apm uninstall team-hooks' again" in text, text
    assert hook.is_symlink() and elsewhere.is_file()
    assert "hooks" in _pending_types(consumer)["team-hooks"]


def test_uninstall_bundle_keeps_a_script_an_edited_hook_still_runs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4679 review: a bundle hook whose matcher was edited keeps its script and claim.

    The edited entry no longer matches what the bundle deployed, so retirement
    leaves it registered; deleting the script it runs would leave a broken hook
    behind a successful uninstall. The script and its claim stay and the
    config is named for repair.
    """
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "edited-matcher-consumer", key)
    world.apm(consumer, "install", str(bundle))
    settings = consumer / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    for entry in data["hooks"]["PreToolUse"]:
        if entry.get("matcher") == "Bash":
            entry["matcher"] = "Write"
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    script = ".claude/hooks/hookpkg/hooks/scripts/x.sh"
    assert script in _claims(world, consumer)

    text = output(world.apm(consumer, "uninstall", str(bundle), expect=1))

    assert ".claude/settings.json" in text, text
    assert "Removed hooks from local bundle 'hookpkg'" not in text, text
    assert "Traceback" not in text, text
    assert _hook_entries(consumer) == {"PreToolUse": ["Write"]}
    assert (consumer / script).is_file()
    assert script in _claims(world, consumer)
    # Refused before retirement: the bundle keeps the ownership trust checks read.
    assert "local-bundle::hookpkg" in (consumer / ".claude" / "apm-hooks.json").read_text()


def test_uninstall_bundle_ignores_a_permission_rule_naming_its_script(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4679 review: only hook entries run a script; a permission rule naming it does not."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "permission-rule-consumer", key)
    world.apm(consumer, "install", str(bundle))
    script = ".claude/hooks/hookpkg/hooks/scripts/x.sh"
    settings = consumer / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["permissions"] = {"allow": [f"Read({script})"]}
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    text = output(world.apm(consumer, "uninstall", str(bundle)))

    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    assert not (consumer / script).exists()
    assert script not in _claims(world, consumer)
    kept = json.loads(settings.read_text(encoding="utf-8"))
    assert kept["permissions"] == {"allow": [f"Read({script})"]}


def test_uninstall_user_bundle_with_a_claude_config_outside_home(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4679 review: scripts under a CLAUDE_CONFIG_DIR outside HOME leave with their claims."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    home = world.isolated.home
    claude_root = world.isolated.root / "claude-profile"
    claude_root.mkdir()
    world.env["CLAUDE_CONFIG_DIR"] = str(claude_root)
    write(home / ".apm" / "apm.yml", "name: user\nversion: 0.1.0\ntargets: [claude]\n")
    _allow(home / ".apm", key)
    world.apm(world.isolated.work_root, "install", "-g", str(bundle))
    lock_path = home / ".apm" / "apm.lock.yaml"
    scripts = [
        p for p in load_yaml(lock_path).get("local_deployed_files") or [] if p.endswith("/x.sh")
    ]
    assert scripts and all(Path(p).is_file() for p in scripts), load_yaml(lock_path)

    text = output(world.apm(world.isolated.work_root, "uninstall", "-g", "hookpkg"))

    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    claims = load_yaml(lock_path).get("local_deployed_files") or []
    assert not [p for p in claims if p.endswith("/x.sh")], claims
    assert not any(Path(p).exists() for p in scripts), scripts


def test_uninstall_retry_by_bundle_path_finishes_an_aliased_bundle_record(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5003: a bundle installed with --as is retried by its path after a failed record update."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "alias-retry-consumer", key)
    world.apm(consumer, "install", str(bundle), "--as", "team-hooks")
    assert "hooks" in _pending_types(consumer)["team-hooks"]

    _source_apm(world, consumer, "uninstall", str(bundle), expect=1)
    assert _hook_entries(consumer) == {}
    assert "hooks" in _pending_types(consumer)["team-hooks"]

    before = tree_bytes(consumer)
    retried = output(world.apm(consumer, "uninstall", str(bundle)))
    assert "Removed hooks from local bundle 'team-hooks'" in retried, retried
    after = tree_bytes(consumer)
    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    assert changed == {"apm_modules/.local-bundle-pending.json"}, changed
    assert "hooks" not in _pending_types(consumer).get("team-hooks", [])


def test_uninstall_retry_by_bundle_path_refuses_two_aliased_copies(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5003 control: two retiring copies of one bundle stay ambiguous by path."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "alias-twins-consumer", key)
    world.apm(consumer, "install", str(bundle), "--as", "team-a")
    world.apm(consumer, "install", str(bundle), "--as", "team-b")
    for slug in ("team-a", "team-b"):
        _source_apm(world, consumer, "uninstall", slug, expect=1, locked=slug)
    assert {"team-a", "team-b"} <= {
        slug for slug, types in _pending_types(consumer).items() if "hooks" in types
    }
    before = tree_bytes(consumer)

    refused = " ".join(output(world.apm(consumer, "uninstall", str(bundle), expect=1)).split())

    assert "matches 2 installed local bundles (team-a, team-b)" in refused, refused
    assert tree_bytes(consumer) == before
    world.apm(consumer, "uninstall", "team-a")


def test_uninstall_bundle_ignores_a_matcher_naming_its_script(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4679 review: only a hook's command fields run a script; a matcher naming it does not."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "matcher-text-consumer", key)
    world.apm(consumer, "install", str(bundle))
    script = ".claude/hooks/hookpkg/hooks/scripts/x.sh"
    settings = consumer / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    mine = {
        "matcher": "hooks/hookpkg/hooks/scripts/x.sh",
        "hooks": [{"type": "command", "command": "true"}],
    }
    data["hooks"].setdefault("Stop", []).append(mine)
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    text = output(world.apm(consumer, "uninstall", str(bundle)))

    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    assert not (consumer / script).exists()
    assert script not in _claims(world, consumer)
    assert _hook_entries(consumer) == {"Stop": ["hooks/hookpkg/hooks/scripts/x.sh"]}


def test_uninstall_bundle_by_name_reaches_a_gate_off_partial_retirement(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4898: a gate-off bundle whose native entries are gone is still selected by name."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, _key = _packed_bundle(world, tmp_path)
    consumer = world.consumer("gate-off-partial-consumer", targets=("claude",))
    world.apm(consumer, "install", str(bundle))
    write(consumer / ".claude" / "settings.json", "{}\n")
    script = ".claude/hooks/hookpkg/hooks/scripts/x.sh"
    assert script in _claims(world, consumer)

    text = output(world.apm(consumer, "uninstall", "hookpkg"))
    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    assert not (consumer / script).exists()
    assert script not in _claims(world, consumer)


def test_uninstall_bundle_path_finds_a_partially_retired_alias(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4898: an alias whose native entries are gone is matched by the key its sidecar keeps."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "partial-alias-consumer", key)
    world.apm(consumer, "install", str(bundle), "--as", "team-hooks")
    write(consumer / ".claude" / "settings.json", "{}\n")
    script = ".claude/hooks/team-hooks/hooks/scripts/x.sh"
    assert script in _claims(world, consumer)

    text = output(world.apm(consumer, "uninstall", str(bundle)))
    assert "Removed hooks from local bundle 'team-hooks'" in text, text
    sidecar = consumer / ".claude" / "apm-hooks.json"
    assert not sidecar.exists() or "local-bundle::team-hooks" not in sidecar.read_text()
    assert script not in _claims(world, consumer)


def test_uninstall_bundle_removes_kiro_scripts_and_claims(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4898: Kiro copies scripts to .kiro/hooks/<slug>/; uninstall removes them and their claims."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "kiro-consumer", key)
    (consumer / ".kiro").mkdir()
    world.apm(consumer, "install", str(bundle), "--target", "claude,kiro")
    kiro_scripts = [c for c in _claims(world, consumer) if c.startswith(".kiro/hooks/hookpkg/")]
    assert kiro_scripts, _claims(world, consumer)

    text = output(world.apm(consumer, "uninstall", str(bundle)))
    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    assert not [c for c in _claims(world, consumer) if c.startswith(".kiro/hooks/")]
    assert not (consumer / ".kiro" / "hooks" / "hookpkg").exists()


def test_uninstall_bundle_keeps_kiro_scripts_an_unrecorded_hook_file_runs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4903 review: without its Kiro record, no kept hook file runs a deleted script."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "kiro-no-record-consumer", key)
    (consumer / ".kiro").mkdir()
    world.apm(consumer, "install", str(bundle), "--target", "claude,kiro")
    kiro_scripts = [c for c in _claims(world, consumer) if c.startswith(".kiro/hooks/hookpkg/")]
    assert kiro_scripts, _claims(world, consumer)
    hooks_dir = consumer / ".kiro" / "hooks"

    def runs(path: Path) -> list[str]:
        body = path.read_text(encoding="utf-8").replace("\\", "/")
        return [c for c in kiro_scripts if c.removeprefix(".kiro/") in body]

    descriptors = [p for p in hooks_dir.glob("*.json") if runs(p)]
    assert descriptors, sorted(hooks_dir.iterdir())
    (consumer / ".kiro" / "apm-hooks.json").unlink()

    result = world.apm(consumer, "uninstall", "hookpkg", expect=None)
    text = output(result)

    assert "Traceback" not in text, text
    claims = _claims(world, consumer)
    for path in (p for p in descriptors if p.is_file()):
        # A hook file left registered keeps every script it runs, and is named.
        assert all((consumer / c).is_file() and c in claims for c in runs(path)), text
        assert result.returncode == 1 and path.name in text, text
        assert "Removed hooks from local bundle 'hookpkg'" not in text, text


def test_uninstall_bundle_keeps_kiro_scripts_beside_an_unreadable_hook_file(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4903 review: an unreadable per-file hook file may run the scripts; they stay."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "kiro-unreadable-consumer", key)
    (consumer / ".kiro").mkdir()
    world.apm(consumer, "install", str(bundle), "--target", "claude,kiro")
    kiro_scripts = [c for c in _claims(world, consumer) if c.startswith(".kiro/hooks/hookpkg/")]
    assert kiro_scripts, _claims(world, consumer)
    write(consumer / ".kiro" / "hooks" / "stray.json", "{not json\n")

    text = output(world.apm(consumer, "uninstall", "hookpkg", expect=1))

    assert ".kiro/hooks/stray.json" in text, text
    assert "Removed hooks from local bundle 'hookpkg'" not in text, text
    assert "Traceback" not in text, text
    claims = _claims(world, consumer)
    assert all((consumer / c).is_file() and c in claims for c in kiro_scripts), claims


def test_uninstall_bundle_refuses_a_sidecar_record_with_malformed_hook_files(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5144: ``hook_files: {}`` is a corrupted record, never "no hook files"; refuse."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    consumer, _hook = _copilot_bundle_consumer(world, tmp_path, "malformed-hook-files-consumer")
    sidecar = consumer / ".github" / "apm-hooks.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    (record,) = data["local-bundle-hooks"]
    record["hook_files"] = {}
    write(sidecar, json.dumps(data))
    before = tree_bytes(consumer)

    refused = output(world.apm(consumer, "uninstall", "hookpkg", expect=1))
    assert ".github/apm-hooks.json (unreadable)" in refused, refused
    assert "Traceback" not in refused, refused
    assert tree_bytes(consumer) == before


def test_install_retires_nothing_from_a_corrupt_sidecar_and_names_it(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5144 review: reconciliation reads the sidecar first; nothing is unlinked."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    consumer, hook = _copilot_bundle_consumer(world, tmp_path, "corrupt-revoked-consumer")
    sidecar = consumer / ".github" / "apm-hooks.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    (record,) = data["local-bundle-hooks"]
    record["hook_files"] = {}
    write(sidecar, json.dumps(data))
    _allow(consumer, "sha256:" + "0" * 64)  # the gate stays on; the bundle is revoked
    deployed = {
        path: (consumer / path).read_bytes()
        for path in [hook, *(c for c in _claims(world, consumer) if c.endswith("/x.sh"))]
    }
    sidecar_bytes = sidecar.read_bytes()
    dep = _hooked_package(world.isolated.work_root / "otherpkg", "otherpkg", "Write", "os")

    # Installing anything reconciles bundle hooks the trust no longer admits.
    result = world.apm(consumer, "install", str(dep), "--no-policy", expect=None)

    text = output(result)
    assert result.returncode != 0, text
    assert ".github/apm-hooks.json (unreadable)" in text, text
    assert "Traceback" not in text, text
    assert {path: (consumer / path).read_bytes() for path in deployed} == deployed
    assert sidecar.read_bytes() == sidecar_bytes
    assert all(path in _claims(world, consumer) for path in deployed)


def test_uninstall_bundle_refuses_a_malformed_per_file_sidecar_before_writing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4898: a per-file sidecar whose bundle records are malformed is refused up front."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "malformed-sidecar-consumer", key)
    world.apm(consumer, "install", str(bundle))
    write(consumer / ".github" / "apm-hooks.json", json.dumps({"local-bundle-hooks": {}}))
    before = tree_bytes(consumer)

    refused = output(world.apm(consumer, "uninstall", str(bundle), expect=1))
    assert ".github/apm-hooks.json (unreadable)" in refused, refused
    assert "Traceback" not in refused, refused
    assert tree_bytes(consumer) == before


def test_uninstall_bundle_path_refuses_a_keyed_and_a_keyless_copy(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4898: a keyed alias plus a gate-off default copy of one bundle is ambiguous by path."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = _approved_consumer(world, "mixed-identity-consumer", key)
    world.apm(consumer, "install", str(bundle), "--as", "team-hooks")
    manifest = load_yaml(consumer / "apm.yml")
    manifest.pop("executables")
    dump_yaml(manifest, consumer / "apm.yml")
    world.apm(consumer, "install", str(bundle))
    assert _hook_entries(consumer) == {"PreToolUse": ["Bash", "Bash"]}
    before = tree_bytes(consumer)

    refused = output(world.apm(consumer, "uninstall", str(bundle), expect=1))
    assert "hookpkg, team-hooks" in refused, refused
    assert "Run 'apm uninstall <installed-name>'" in refused, refused
    assert tree_bytes(consumer) == before


def _user_hook_naming_the_script(consumer: Path, document_for) -> str:
    """Write an unowned per-file hook built around the bundle's script; return the script."""
    (script,) = [c for c in _claims_of(consumer) if c.endswith("/x.sh")]
    write(consumer / ".github" / "hooks" / "user.json", json.dumps(document_for(script)))
    return script


def _claims_of(consumer: Path) -> list[str]:
    return list(load_yaml(consumer / "apm.lock.yaml").get("local_deployed_files") or [])


def test_uninstall_bundle_ignores_a_script_named_only_in_hook_metadata(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5136: only handler command fields run a script; env metadata naming it does not."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    consumer, _hook = _copilot_bundle_consumer(world, tmp_path, "metadata-consumer")
    script = _user_hook_naming_the_script(
        consumer,
        lambda path: {
            "version": 1,
            "hooks": {
                "preToolUse": [{"type": "command", "bash": "echo safe", "env": {"command": path}}]
            },
        },
    )

    text = output(world.apm(consumer, "uninstall", "hookpkg"))

    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    assert not (consumer / script).exists()
    assert script not in _claims_of(consumer)


def test_uninstall_bundle_keeps_a_script_a_user_hook_handler_runs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5136 control: a handler that runs the script still keeps it and refuses."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    consumer, _hook = _copilot_bundle_consumer(world, tmp_path, "handler-consumer")
    script = _user_hook_naming_the_script(
        consumer,
        lambda path: {"version": 1, "hooks": {"preToolUse": [{"type": "command", "bash": path}]}},
    )

    text = output(world.apm(consumer, "uninstall", "hookpkg", expect=1))

    assert ".github/hooks/user.json" in text, text
    assert "Traceback" not in text, text
    assert (consumer / script).is_file()
    assert script in _claims_of(consumer)


def test_uninstall_bundle_refuses_a_hook_shape_the_grammar_does_not_recognize(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5136: an unrecognized hook shape naming the script fails closed, as before."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    consumer, _hook = _copilot_bundle_consumer(world, tmp_path, "unknown-shape-consumer")
    script = _user_hook_naming_the_script(
        consumer, lambda path: {"hooks": [{"action": {"type": "command", "command": path}}]}
    )

    text = output(world.apm(consumer, "uninstall", "hookpkg", expect=1))

    assert ".github/hooks/user.json" in text, text
    assert (consumer / script).is_file()


def test_uninstall_bundle_ignores_a_user_hook_whose_cwd_is_elsewhere(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5281 control: ``./x.sh`` in an unrelated cwd runs another script; uninstall goes on."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    consumer, _hook = _copilot_bundle_consumer(world, tmp_path, "cwd-elsewhere-consumer")
    script = _user_hook_naming_the_script(
        consumer,
        lambda path: {
            "version": 1,
            "hooks": {"preToolUse": [{"type": "command", "bash": "./x.sh", "cwd": "tools"}]},
        },
    )

    text = output(world.apm(consumer, "uninstall", "hookpkg"))

    assert "Removed hooks from local bundle 'hookpkg'" in text, text
    assert not (consumer / script).exists()
    assert script not in _claims_of(consumer)


def test_uninstall_bundle_refuses_a_user_hook_whose_cwd_escapes_the_project(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5281: a cwd outside the project cannot be cleared; uninstall fails closed."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    consumer, _hook = _copilot_bundle_consumer(world, tmp_path, "cwd-escape-consumer")
    script = _user_hook_naming_the_script(
        consumer,
        lambda path: {
            "version": 1,
            "hooks": {"preToolUse": [{"type": "command", "bash": "./x.sh", "cwd": "../elsewhere"}]},
        },
    )

    text = output(world.apm(consumer, "uninstall", "hookpkg", expect=1))

    assert ".github/hooks/user.json" in text, text
    assert "Traceback" not in text, text
    assert (consumer / script).is_file()
    assert script in _claims_of(consumer)
