"""Files a remote plugin lists from its own repository lock its commit (#5525).

PROMISE: P-LAW-source.ref_cache_coherent -- materialized content, resolved
references and cache identity describe the same source revision (source:
tests/fixtures/lifecycle_bug_ledger/property_catalog/source.ref_cache_coherent.json).
A marketplace plugin whose ``apm.yml`` lists ``./prompts/x.prompt.md`` installs
each file as a virtual dependency on the plugin's repository; that file is
fetched at the commit the plugin resolved at and its lock row records it
(deps/same_repo_pin.py), so no install warns ``cache-pin`` for it.

CELL: OP=install|update SRC=git(GitLab-shaped, no ref) PRIM=prompt+agent
LOCK=absent|0.39.0-shape|current. The origin is a local bare repository served
under ``https://gitlab.com/acme/agents-marketplace`` through git ``insteadOf``;
every HTTP request goes to a dead proxy, so the only egress is git to that
bare repository. The consumer ``apm.yml`` is the shape
``apm install acme-guild@acme`` writes (``git:`` + ``path:``, no ``ref``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.scenarios._kit import assert_changes, changed_paths
from tests.utils.artifact_snapshot import ArtifactSnapshotSet

pytestmark = [
    pytest.mark.promise("P-INV-I1", "P-INV-I2", "P-INV-I3", "P-LAW-source.ref_cache_coherent")
]

REMOTE = "https://gitlab.com/acme/agents-marketplace"
LOOSE = "https://gitlab.com/acme/loose-prompts"
PLUGIN = "plugins/guild"
OTHER = "plugins/other"
PROMPT = f"{PLUGIN}/prompts/review.prompt.md"
AGENT = f"{PLUGIN}/agents/reviewer.agent.md"
DEPLOYED_PROMPT = ".github/prompts/review.prompt.md"
DEPLOYED_AGENT = ".github/agents/reviewer.agent.md"
CACHE_PIN = "cache-pin"
INSTALL_WRITES = (
    "project:apm.lock.yaml",
    "project:.gitignore",
    "project:.apm-local*",
    "project:apm_modules*",
    "project:.github*",
)


def _git(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=APM Test", "-c", "user.email=apm@example.com", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _prompt(label: str) -> str:
    return f"---\ndescription: review\n---\n\nReview {label}.\n"


@dataclass
class _World:
    """A driver world: one project, one isolated home, git routed to local origins."""

    root: Path
    project: Path
    env: dict[str, str]
    origin: Path
    work: Path

    def head(self) -> str:
        return _git("--git-dir", str(self.origin), "rev-parse", "refs/heads/main")

    def publish(self, label: str) -> str:
        (self.work / PROMPT).write_text(_prompt(label), encoding="ascii")
        _git("commit", "-qam", label, cwd=self.work)
        _git("push", "-q", str(self.origin), "HEAD:main", cwd=self.work)
        return self.head()

    def run(self, argv: str):
        return driver.run(self, argv)

    def rows(self) -> dict[str, dict]:
        data = yaml.safe_load((self.project / "apm.lock.yaml").read_text(encoding="utf-8"))
        return {
            f"{row['repo_url']}/{row.get('virtual_path') or ''}".rstrip("/"): row
            for row in data["dependencies"]
        }

    def snapshot(self) -> ArtifactSnapshotSet:
        return ArtifactSnapshotSet.capture({"project": self.project})


def _origin(root: Path, name: str, files: dict[str, str]) -> tuple[Path, Path]:
    work = root / f"{name}-src"
    for relative, body in files.items():
        (work / relative).parent.mkdir(parents=True, exist_ok=True)
        (work / relative).write_text(body, encoding="ascii")
    _git("init", "-q", "-b", "main", str(work))
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "v1", cwd=work)
    origin = root / f"{name}.git"
    _git("clone", "-q", "--bare", str(work), str(origin))
    return work, origin


def _world(tmp_path: Path, *, loose: bool = False):
    plugin_manifest = (
        "name: acme-guild\nversion: 1.0.0\ndependencies:\n  apm:\n"
        "    - ./prompts/review.prompt.md\n    - ./agents/reviewer.agent.md\n"
    )
    work, origin = _origin(
        tmp_path,
        "marketplace",
        {
            f"{PLUGIN}/apm.yml": plugin_manifest,
            PROMPT: _prompt("v1"),
            AGENT: "---\ndescription: reviewer\n---\n\nYou review.\n",
            # A second plugin that lists the first plugin's prompt.
            f"{OTHER}/apm.yml": (
                "name: acme-other\nversion: 1.0.0\ndependencies:\n  apm:\n"
                "    - ../guild/prompts/review.prompt.md\n"
            ),
        },
    )
    rewrites = {f"url.{origin.as_uri()}.insteadOf": (f"{REMOTE}.git", REMOTE)}
    declared = [{"git": f"{REMOTE}.git", "path": PLUGIN}]
    if loose:
        _, loose_origin = _origin(tmp_path, "loose", {"prompts/loose.prompt.md": _prompt("loose")})
        rewrites[f"url.{loose_origin.as_uri()}.insteadOf"] = (f"{LOOSE}.git", LOOSE)
        declared.append({"git": f"{LOOSE}.git", "path": "prompts/loose.prompt.md"})
    config = [(key, value) for key, values in rewrites.items() for value in values]

    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("", encoding="ascii")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "APM_", "GITHUB_", "GH_", "GITLAB_"))
        and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    }
    env.update(
        {
            "HOME": str(home),
            "APM_HOME": str(home / ".apm"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
            "GIT_TERMINAL_PROMPT": "0",
            # Every HTTP request fails fast; only git reaches the local origins.
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "GIT_CONFIG_COUNT": str(len(config)),
        }
    )
    for index, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value

    project = tmp_path / "project"
    project.mkdir()
    manifest = {
        "name": "consumer",
        "version": "1.0.0",
        "targets": ["copilot"],
        "dependencies": {"apm": declared},
    }
    (project / "apm.yml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return _World(tmp_path, project, env, origin, work)


def _assert_pinned_to(world: _World, commit: str) -> None:
    rows = world.rows()
    keys = [f"acme/agents-marketplace/{path}" for path in (PLUGIN, PROMPT, AGENT)]
    assert {key: rows[key].get("resolved_commit") for key in keys} == dict.fromkeys(keys, commit)


def _assert_no_cache_pin(result) -> None:
    combined = result.stdout + result.stderr
    assert CACHE_PIN not in combined, combined


def test_self_listed_files_lock_the_plugin_commit(tmp_path: Path) -> None:
    world = _world(tmp_path)
    head = world.head()

    before = world.snapshot()
    result = world.run("apm install")
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_no_cache_pin(result)
    _assert_pinned_to(world, head)
    assert_changes(changed_paths(before, world.snapshot()), INSTALL_WRITES, label="install")
    assert (world.project / DEPLOYED_PROMPT).read_text(encoding="ascii") == _prompt("v1")
    assert (world.project / DEPLOYED_AGENT).is_file()

    # I3: audit --ci and install --frozen agree on the pinned lock.
    audit = world.run("apm audit --ci")
    assert audit.returncode == 0, audit.stdout + audit.stderr
    assert "no resolved_commit" not in audit.stdout + audit.stderr
    frozen = world.run("apm install --frozen")
    assert frozen.returncode == 0, frozen.stdout + frozen.stderr
    _assert_no_cache_pin(frozen)

    # I2: a converged install writes nothing and still does not warn.
    lock_bytes = (world.project / "apm.lock.yaml").read_bytes()
    before = world.snapshot()
    again = world.run("apm install")
    assert again.returncode == 0, again.stdout + again.stderr
    _assert_no_cache_pin(again)
    assert changed_paths(before, world.snapshot()) == frozenset()
    assert (world.project / "apm.lock.yaml").read_bytes() == lock_bytes


def test_update_moves_the_files_with_the_plugin(tmp_path: Path) -> None:
    world = _world(tmp_path)
    assert world.run("apm install").returncode == 0
    new_head = world.publish("v2")

    result = world.run("apm update --yes")
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_no_cache_pin(result)
    _assert_pinned_to(world, new_head)
    assert (world.project / DEPLOYED_PROMPT).read_text(encoding="ascii") == _prompt("v2")


def test_files_come_from_the_plugin_commit_when_the_branch_moves(tmp_path: Path) -> None:
    # The client's first fetch or clone is the plugin's; right after it the
    # origin's main advances, before any file is fetched.
    staged = tmp_path / "staged-head"
    moved = tmp_path / "moved"
    origin = tmp_path / "marketplace.git"
    real_git = shutil.which("git")
    assert real_git
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'"{real_git}" "$@"\nstatus=$?\n'
        'case " $* " in *" fetch "*|*" clone "*)\n'
        f'  if [ ! -e "{moved}" ]; then\n'
        f'    touch "{moved}"\n'
        f'    "{real_git}" --git-dir="{origin}" update-ref refs/heads/main "$(cat "{staged}")"\n'
        "  fi;;\n"
        "esac\n"
        "exit $status\n",
        encoding="ascii",
    )
    wrapper.chmod(0o755)
    world = _world(tmp_path)
    world.env["PATH"] = f"{wrapper_dir}{os.pathsep}{world.env.get('PATH', '')}"
    plugin_commit = world.head()
    staged.write_text(world.publish("v2"), encoding="ascii")
    _git("--git-dir", str(world.origin), "update-ref", "refs/heads/main", plugin_commit)

    result = world.run("apm install")
    assert result.returncode == 0, result.stdout + result.stderr
    assert moved.exists(), "the plugin fetch never ran through git"
    assert world.head() != plugin_commit
    _assert_no_cache_pin(result)
    _assert_pinned_to(world, plugin_commit)
    assert (world.project / DEPLOYED_PROMPT).read_text(encoding="ascii") == _prompt("v1")


def test_lock_written_without_file_commits_heals_on_install(tmp_path: Path) -> None:
    world = _world(tmp_path)
    head = world.head()
    assert world.run("apm install").returncode == 0
    # The rows 0.39.0 wrote: no resolved_ref and no resolved_commit on the files.
    lock_path = world.project / "apm.lock.yaml"
    data = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    for row in data["dependencies"]:
        if row.get("virtual_path") in {PROMPT, AGENT}:
            row.pop("resolved_commit", None)
            row.pop("resolved_ref", None)
    lock_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    # --frozen never rewrites the lock; the old rows stay installable.
    frozen = world.run("apm install --frozen")
    assert frozen.returncode == 0, frozen.stdout + frozen.stderr

    result = world.run("apm install")
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_no_cache_pin(result)
    _assert_pinned_to(world, head)
    audit = world.run("apm audit --ci")
    assert audit.returncode == 0, audit.stdout + audit.stderr


def test_two_plugins_sharing_a_file_at_different_commits_install(tmp_path: Path) -> None:
    # The second plugin resolves at a newer commit than the locked first one;
    # an inherited commit is not a user pin, so the shared file is no conflict.
    world = _world(tmp_path)
    first = world.head()
    assert world.run("apm install").returncode == 0
    second = world.publish("v2")
    manifest = yaml.safe_load((world.project / "apm.yml").read_text(encoding="utf-8"))
    manifest["dependencies"]["apm"].append({"git": f"{REMOTE}.git", "path": OTHER})
    (world.project / "apm.yml").write_text(yaml.safe_dump(manifest, sort_keys=False), "utf-8")

    result = world.run("apm install")
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_no_cache_pin(result)
    rows = world.rows()
    assert rows[f"acme/agents-marketplace/{PLUGIN}"]["resolved_commit"] == first
    assert rows[f"acme/agents-marketplace/{OTHER}"]["resolved_commit"] == second
    # One shared row; its commit names the bytes deployed from it.
    shared = rows[f"acme/agents-marketplace/{PROMPT}"]["resolved_commit"]
    label = {first: "v1", second: "v2"}[shared]
    assert (world.project / DEPLOYED_PROMPT).read_text(encoding="ascii") == _prompt(label)


def test_a_file_with_no_known_commit_still_warns(tmp_path: Path) -> None:
    # Negative control: a file declared directly on a GitLab-shaped repository
    # with no ref has no commit APM can learn offline, so it stays unpinned.
    world = _world(tmp_path, loose=True)

    result = world.run("apm install")
    assert result.returncode == 0, result.stdout + result.stderr
    warnings = [line for line in (result.stdout + result.stderr).splitlines() if CACHE_PIN in line]
    assert len(warnings) == 1 and "acme/loose-prompts" in warnings[0], warnings
    _assert_pinned_to(world, world.head())
    assert not world.rows()["acme/loose-prompts/prompts/loose.prompt.md"].get("resolved_commit")
