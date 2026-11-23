"""Real-CLI lifecycle: `apm uninstall --trust-lockfile` consent path (#4295).

Uninstall deletes a deployed file only with deployment evidence (req-lk-024).
When no receipt exists and no locked render can be produced (the source is
offline, private or deleted and the checkout and cache are gone), the
refusal previews which files the flag would delete. `--trust-lockfile` then
deletes exactly those whose bytes equal the hash apm.lock.yaml records, keeps
and names every other file and finishes the uninstall. It never overrides
evidence that a file differs.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.skill_selection_world import (
    AUDIT_CI,
    INSTALL,
    SkillSelectionWorld,
    lock_path,
    locked,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.promise("P-OWN-file-level-deploy-sync-cleanup"),
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_SKILLS = ("alpha", "beta", "gamma")
_SKILLS_ROOT = Path(".agents") / "skills"
_GLOBAL_INSTALL = ("install", "-g", "--no-policy", "--parallel-downloads", "0")


def _tree(*roots: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for root in roots:
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in candidates:
            if path.is_file():
                found[path.as_posix()] = path.read_bytes()
    return found


def _text(result) -> str:
    return result.stdout + result.stderr


def _edit(path: Path) -> None:
    path.write_text(path.read_text(encoding="utf-8") + "\nmy edit\n", encoding="utf-8")


def _remove_receipts(world: SkillSelectionWorld) -> None:
    for receipts in world.isolated.config_root.glob("deployment-receipts*"):
        shutil.rmtree(receipts)


def _strand(world: SkillSelectionWorld, modules: Path) -> None:
    """No receipt, no checkout, no cache, no reachable source."""
    _remove_receipts(world)
    shutil.rmtree(modules / "fixture-org" / "pkg")
    shutil.rmtree(world.isolated.cache_root, ignore_errors=True)
    world.isolated.cache_root.mkdir(parents=True)
    (published,) = world.published.values()
    origin = published.repository.origin
    origin.rename(origin.with_name(origin.name + ".gone"))


class _Scope:
    def __init__(self, world: SkillSelectionWorld, slug: str, *, user: bool) -> None:
        self.user = user
        if user:
            self.apm_root = world.isolated.config_root
            self.cwd = world.isolated.work_root
            self.skills = world.isolated.home / _SKILLS_ROOT
            dump_yaml(
                {
                    "name": "user",
                    "version": "0.1.0",
                    "description": "user scope manifest",
                    "author": "APM Test",
                    "dependencies": {"apm": [slug]},
                    "targets": ["copilot"],
                },
                self.apm_root / "apm.yml",
            )
        else:
            self.apm_root = world.project("consumer", dependencies=[slug])
            self.cwd = self.apm_root
            self.skills = self.apm_root / _SKILLS_ROOT

    def args(self, *args: str) -> tuple[str, ...]:
        return (args[0], "-g", *args[1:]) if self.user else args

    @property
    def install(self) -> tuple[str, ...]:
        return _GLOBAL_INSTALL if self.user else INSTALL


def _installed(tmp_path: Path, apm_binary_path: Path, *, user: bool):
    world = SkillSelectionWorld.create(tmp_path / "w", (str(apm_binary_path),))
    slug = world.publish("pkg", skills=_SKILLS).slug
    scope = _Scope(world, slug, user=user)
    return world, slug, scope


@pytest.mark.parametrize("user", [False, True], ids=["project", "user"])
def test_trust_lockfile_deletes_locked_bytes_and_keeps_an_edit_when_the_source_is_gone(
    tmp_path: Path, apm_binary_path: Path, user: bool
) -> None:
    world, slug, scope = _installed(tmp_path, apm_binary_path, user=user)
    with world.serve() as env:
        world.run(scope.install, cwd=scope.cwd, env=env, scenario_id="4295-install")
    edited = scope.skills / "alpha" / "SKILL.md"
    _edit(edited)
    _strand(world, scope.apm_root / "apm_modules")
    watched = (scope.apm_root / "apm.yml", lock_path(scope.apm_root), scope.skills)
    before = _tree(*watched)

    refused = world.run(
        scope.args("uninstall", slug), cwd=scope.cwd, env=env, expect=None, scenario_id="4295-r"
    )
    text = _text(refused)
    assert refused.returncode == 1, text
    assert "Uninstall refused" in text, text
    assert _tree(*watched) == before
    marks = {
        name: line
        for line in text.splitlines()
        for name in _SKILLS
        if f"skills/{name}/SKILL.md:" in line
    }
    for name in ("beta", "gamma"):
        assert marks[name].endswith("--trust-lockfile would delete it"), text
    assert marks["alpha"].endswith("kept even with --trust-lockfile"), text
    # The printed remedy is the command run next, exactly as printed.
    printed = re.search(r"'(apm uninstall [^']*--trust-lockfile)'", text.splitlines()[-1])
    assert printed, text
    argv = tuple(shlex.split(printed.group(1))[1:])
    assert argv == scope.args("uninstall", slug, "--trust-lockfile"), text

    trusted = world.run(
        argv,
        cwd=scope.cwd,
        env=env,
        expect=None,
        scenario_id="4295-t",
    )
    text = _text(trusted)
    assert trusted.returncode == 0, text
    for name in ("beta", "gamma"):
        assert not (scope.skills / name).exists(), text
        assert text.count(f"Deleted {_SKILLS_ROOT.as_posix()}/{name}/SKILL.md") == 1, text
    assert edited.is_file() and "my edit" in edited.read_text(encoding="utf-8")
    assert "alpha/SKILL.md" in text, text
    assert slug not in json.dumps(load_yaml(scope.apm_root / "apm.yml"))
    assert not lock_path(scope.apm_root).exists() or locked(scope.apm_root, slug) is None

    audit = world.run(
        AUDIT_CI,
        cwd=scope.apm_root,
        env=env,
        expect=None,
        scenario_id="4295-audit",
    )
    if audit.returncode != 0:
        failures = [
            check for check in json.loads(audit.stdout).get("checks", []) if not check.get("passed")
        ]
        details = json.dumps(failures)
        assert "beta" not in details and "gamma" not in details, details


def test_trust_lockfile_keeps_a_user_file_the_lockfile_claims_when_a_render_exists(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A lockfile row and matching hash never outweigh a render that lacks the path."""
    from apm_cli.utils.content_hash import compute_file_hash

    world, slug, scope = _installed(tmp_path, apm_binary_path, user=False)
    mine = scope.skills / "alpha" / "mine.md"
    with world.serve() as env:
        world.run(scope.install, cwd=scope.cwd, env=env, scenario_id="4295-n1-install")
        mine.write_text("Mine.\n", encoding="utf-8")
        lock = load_yaml(lock_path(scope.apm_root))
        claim = f"{_SKILLS_ROOT.as_posix()}/alpha/mine.md"
        for dep in lock["dependencies"]:
            if dep.get("name") == "pkg":
                dep["deployed_files"] = sorted([*dep["deployed_files"], claim])
                dep.setdefault("deployed_file_hashes", {})[claim] = compute_file_hash(mine)
        lock.pop("deployments", None)
        dump_yaml(lock, lock_path(scope.apm_root))
        _remove_receipts(world)
        result = world.run(
            ("uninstall", slug, "--trust-lockfile"),
            cwd=scope.cwd,
            env=env,
            expect=None,
            scenario_id="4295-n1",
        )
    assert result.returncode == 0, _text(result)
    assert mine.read_text(encoding="utf-8") == "Mine.\n"
    assert f"Deleted {claim}" not in _text(result)


def test_trust_lockfile_keeps_a_file_whose_receipt_and_render_both_differ(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, slug, scope = _installed(tmp_path, apm_binary_path, user=False)
    edited = scope.skills / "alpha" / "SKILL.md"
    with world.serve() as env:
        world.run(scope.install, cwd=scope.cwd, env=env, scenario_id="4295-n2-install")
        _edit(edited)
        result = world.run(
            ("uninstall", slug, "--trust-lockfile"),
            cwd=scope.cwd,
            env=env,
            expect=None,
            scenario_id="4295-n2",
        )
    text = _text(result)
    assert result.returncode == 0, text
    assert "my edit" in edited.read_text(encoding="utf-8")
    assert "Deleted .agents/skills/alpha/SKILL.md" not in text, text
    assert not (scope.skills / "beta").exists(), text


def test_trust_lockfile_deletes_nothing_when_receipt_state_is_unreadable(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, slug, scope = _installed(tmp_path, apm_binary_path, user=False)
    with world.serve() as env:
        world.run(scope.install, cwd=scope.cwd, env=env, scenario_id="4295-n3-install")
    receipts = sorted((world.isolated.config_root / "deployment-receipts").glob("*.json"))
    assert receipts
    _strand(world, scope.apm_root / "apm_modules")
    receipts[0].parent.mkdir(parents=True, exist_ok=True)
    receipts[0].write_text("{not json", encoding="utf-8")
    before = _tree(scope.skills)

    result = world.run(
        ("uninstall", slug, "--trust-lockfile"),
        cwd=scope.cwd,
        env=env,
        expect=None,
        scenario_id="4295-n3",
    )
    text = _text(result)
    assert _tree(scope.skills) == before, text
    assert "Deleted " not in text, text


def test_trust_lockfile_keeps_an_edited_hook_without_failing_the_uninstall(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A hook consent keeps for its edit is named, not counted as a cleanup failure."""
    world = SkillSelectionWorld.create(tmp_path / "w", (str(apm_binary_path),))
    hook = {
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo t"}]}]
        }
    }
    slug = world.publish("pkg", skills=("beta",), hooks={"pretool": hook}).slug
    scope = _Scope(world, slug, user=False)
    with world.serve() as env:
        world.run(scope.install, cwd=scope.cwd, env=env, scenario_id="4295-h-install")
    hooks = [
        path for path in locked(scope.apm_root, slug).get("deployed_files") or [] if "hook" in path
    ]
    assert hooks, locked(scope.apm_root, slug)
    edited = scope.apm_root / hooks[0]
    edited.write_text(edited.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    _strand(world, scope.apm_root / "apm_modules")

    trusted = world.run(
        ("uninstall", slug, "--trust-lockfile"),
        cwd=scope.cwd,
        env=env,
        expect=None,
        scenario_id="4295-h-trust",
    )

    text = _text(trusted)
    assert trusted.returncode == 0, text
    assert edited.is_file() and edited.read_text(encoding="utf-8").endswith("\n\n"), text
    assert not (scope.skills / "beta").exists(), text
    assert slug not in json.dumps(load_yaml(scope.apm_root / "apm.yml"))
