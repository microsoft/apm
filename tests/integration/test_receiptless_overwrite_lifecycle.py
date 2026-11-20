"""Installed-CLI contract: install replaces a managed file only with deployment evidence (#4360).

A managed instruction, agent, prompt, command or rule whose bytes differ
from what APM renders is replaced only when this checkout holds a
deployment receipt for it, or its bytes equal the locked render
(req-lk-024). A clone, or a project last installed by an APM that wrote no
receipts, has neither for a user-edited file: install keeps it, names it
with the remedy, keeps its prior lock entry and exits 1. A lockfile hash
that matches the edited bytes proves nothing. Unedited files adopt
silently.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-LAW-ownership.preserve_unowned", "P-LAW-outcome.status_matches_state"),
]

_EDIT = "\nLocal edit.\n"
# One deployed file per kind; claude carries commands and rules.
_KINDS = {
    "instruction": "{copilot}/instructions/style.instructions.md",
    "agent": "{copilot}/agents/helper.agent.md",
    "prompt": "{copilot}/prompts/review.prompt.md",
    "command": ".claude/commands/review.md",
    "rule": ".claude/rules/style.md",
}


@dataclass
class _World:
    root: Path  # the project, or HOME for -g
    home: Path
    runner: ApmLifecycleRunner
    env: dict[str, str]
    scope: tuple[str, ...]
    repositories: LocalGitRepositoryFactory | None = None
    repository: LocalGitRepository | None = None

    def install(self, *args: str) -> CommandResult:
        return self.runner.run(("install", *self.scope, *args), cwd=self.root, env=self.env)

    @property
    def lock_path(self) -> Path:
        return self.home / ".apm" / "apm.lock.yaml" if self.scope else self.root / "apm.lock.yaml"

    def kind_paths(self) -> dict[str, Path]:
        copilot = ".copilot" if self.scope else ".github"
        return {
            kind: self.root / pattern.format(copilot=copilot) for kind, pattern in _KINDS.items()
        }

    def forget_receipts(self) -> None:
        """Leave the project as a clone or a receipt-less APM would."""
        shutil.rmtree(self.home / ".apm" / "deployment-receipts", ignore_errors=True)
        shutil.rmtree(self.root / ".apm-local", ignore_errors=True)
        assert not (self.home / ".apm" / "deployment-receipts").exists()


def _write_package(root: Path) -> None:
    files = {
        "apm.yml": "name: kit\nversion: 1.0.0\n",
        ".apm/instructions/style.instructions.md": "---\napplyTo: '**'\n---\n# Style\n",
        ".apm/agents/helper.agent.md": "---\nname: helper\ndescription: Helper\n---\n# Helper\n",
        ".apm/prompts/review.prompt.md": "---\ndescription: Review\n---\n# Review\n",
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")


def _world(tmp_path: Path, apm_binary_path: Path, *, user_scope: bool, git: bool = False) -> _World:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    source = isolated.package_root / "kit"
    _write_package(source)
    dependency: object = str(source)
    repositories = repository = None
    if git:
        remote = "https://github.com/test/kit.git"
        repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
        repository = repositories.create("kit", source_tree=source)
        dependency = {"git": remote, "ref": repositories.commit(repository, message="v1").sha}
        env = repositories.url_rewrite_subprocess_env(repository, remote)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    if user_scope:
        (isolated.home / ".claude").mkdir()
        world = _World(isolated.home, isolated.home, runner, env, ("-g",))
        result = world.install(str(source), "--target", "copilot,claude")
    else:
        project = isolated.work_root / "consumer"
        project.mkdir()
        dump_yaml(
            {
                "name": "consumer",
                "version": "1.0.0",
                "targets": ["copilot", "claude"],
                "dependencies": {"apm": [dependency]},
            },
            project / "apm.yml",
        )
        world = _World(project, isolated.home, runner, env, (), repositories, repository)
        result = world.install()
    assert result.returncode == 0, result.stdout + result.stderr
    return world


def _entry(world: _World, path: Path) -> str:
    return path.relative_to(world.root).as_posix()


def _locked_hashes(world: _World) -> dict[str, str]:
    data = load_yaml(world.lock_path)
    (dependency,) = data["dependencies"]
    return dict(dependency["deployed_file_hashes"])


def _kept_line(entry: str, *, user_scope: bool) -> str:
    install = "apm install -g" if user_scope else "apm install"
    return (
        f"Kept {entry}: it differs from what APM renders and there is no local "
        "deployment receipt or locked render proving APM wrote it, so it may hold "
        f"local edits. Move it aside, then run '{install}' to restore it; or run "
        f"'{install} --force' to overwrite it."
    )


def _output(result: CommandResult) -> str:
    return " ".join((result.stdout + result.stderr).split())


@pytest.mark.parametrize("lock", ["recorded", "forged-to-edit"])
@pytest.mark.parametrize("scope", ["project", "user"])
def test_receiptless_install_keeps_each_edited_kind(
    tmp_path: Path, apm_binary_path: Path, scope: str, lock: str
) -> None:
    user_scope = scope == "user"
    world = _world(tmp_path, apm_binary_path, user_scope=user_scope)
    paths = world.kind_paths()
    world.forget_receipts()
    for path in paths.values():
        path.write_text(path.read_text(encoding="utf-8") + _EDIT, encoding="utf-8")
    edited = {path: path.read_bytes() for path in paths.values()}
    if lock == "forged-to-edit":
        # A lock hash equal to the edited bytes is not deployment evidence.
        from apm_cli.utils.content_hash import compute_file_hash

        text = world.lock_path.read_text(encoding="utf-8")
        for path in paths.values():
            # Both recorded copies: the file hash list and the deployment ledger row.
            entry, forged = re.escape(_entry(world, path)), compute_file_hash(path)
            for pattern in (
                rf"(?m)^(    {entry}: )sha256:[0-9a-f]{{64}}$",
                rf"(value: {entry}\n(?:  (?!content_hash).*\n)*?  content_hash: )sha256:[0-9a-f]{{64}}",
            ):
                text, count = re.subn(pattern, lambda match, f=forged: match.group(1) + f, text)
                assert count == 1, (pattern, text)
        world.lock_path.write_text(text, encoding="utf-8")
    hashes_before = _locked_hashes(world)

    for _ in range(2):
        result = world.install()
        output = _output(result)
        assert result.returncode == 1, output
        for path in paths.values():
            assert _kept_line(_entry(world, path), user_scope=user_scope) in output, output
        assert "apm.lock.yaml keeps the previous entries of the 5 kept file(s)." in output
        assert {path: path.read_bytes() for path in paths.values()} == edited
        assert _locked_hashes(world) == hashes_before

    # The printed remedy restores the files.
    for path in paths.values():
        path.unlink()
    restored = world.install()
    assert restored.returncode == 0, _output(restored)
    assert all(_EDIT not in path.read_text(encoding="utf-8") for path in paths.values())


@pytest.mark.parametrize("scope", ["project", "user"])
def test_receiptless_install_adopts_render_equal_files(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    world = _world(tmp_path, apm_binary_path, user_scope=scope == "user")
    paths = world.kind_paths()
    world.forget_receipts()
    before = {path: path.read_bytes() for path in paths.values()}
    lock_before = world.lock_path.read_bytes()

    result = world.install()

    assert result.returncode == 0, _output(result)
    assert "Kept " not in _output(result)
    assert {path: path.read_bytes() for path in paths.values()} == before
    assert world.lock_path.read_bytes() == lock_before


def test_receiptless_upgrade_replaces_files_equal_to_the_locked_render(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The locked render proves an unedited file, so a new version replaces it."""
    world = _world(tmp_path, apm_binary_path, user_scope=False, git=True)
    paths = world.kind_paths()
    kept = paths.pop("instruction")
    world.forget_receipts()
    kept.write_text(kept.read_text(encoding="utf-8") + _EDIT, encoding="utf-8")
    edited = kept.read_bytes()
    assert world.repositories is not None and world.repository is not None
    for relative in (
        ".apm/instructions/style.instructions.md",
        ".apm/agents/helper.agent.md",
        ".apm/prompts/review.prompt.md",
    ):
        source = world.repository.worktree / relative
        source.write_text(source.read_text(encoding="utf-8") + "\nVersion 2.\n", encoding="utf-8")
    sha = world.repositories.commit(world.repository, message="v2").sha
    manifest = load_yaml(world.root / "apm.yml")
    manifest["dependencies"]["apm"][0]["ref"] = sha
    dump_yaml(manifest, world.root / "apm.yml")

    result = world.install()

    output = _output(result)
    assert result.returncode == 1, output
    assert _kept_line(_entry(world, kept), user_scope=False) in output
    assert kept.read_bytes() == edited
    assert all("Version 2." in path.read_text(encoding="utf-8") for path in paths.values())
    assert "This install wrote" in output
    for path in paths.values():
        assert _entry(world, path) in output


def _write_skill_bundle(root: Path) -> Path:
    """A plugin-format bundle with one skill, as ``apm pack`` writes it."""
    import hashlib
    import json

    body = "---\nname: test-skill\ndescription: Test skill\n---\n# Test skill\n"
    relative = "skills/test-skill/SKILL.md"
    (root / relative).parent.mkdir(parents=True)
    (root / relative).write_text(body, encoding="utf-8")
    (root / "plugin.json").write_text(json.dumps({"id": "test-plugin", "name": "Test"}))
    hashes = {relative: hashlib.sha256(body.encode()).hexdigest()}
    dump_yaml(
        {
            "pack": {"format": "plugin", "target": "copilot", "bundle_files": hashes},
            "dependencies": [
                {
                    "repo_url": "owner/test-plugin",
                    "resolved_commit": "abc123",
                    "deployed_files": [relative],
                    "deployed_file_hashes": hashes,
                }
            ],
        },
        root / "apm.lock.yaml",
    )
    return root


@pytest.mark.parametrize("legacy", ["unedited", "edited"])
def test_receiptless_legacy_bundle_skill_migrates_only_when_render_equal(
    tmp_path: Path, apm_binary_path: Path, legacy: str
) -> None:
    """A legacy ``.github/skills`` copy an older APM left without receipts (#4049 residual).

    The converged reinstall deploys the same bundle; legacy bytes equal to
    that render migrate, edited ones are kept, named and exit 1.
    """
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    bundle = _write_skill_bundle(isolated.package_root / "bundle")
    project = isolated.work_root / "consumer"
    (project / ".github").mkdir(parents=True)
    dump_yaml({"name": "consumer", "version": "1.0.0"}, project / "apm.yml")
    world = _World(
        project,
        isolated.home,
        ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180),
        isolated.subprocess_env(),
        (),
    )
    command = (str(bundle), "--target", "copilot")
    world.env["APM_LEGACY_SKILL_PATHS"] = "1"
    seeded = world.install(*command)
    assert seeded.returncode == 0, _output(seeded)
    del world.env["APM_LEGACY_SKILL_PATHS"]
    legacy_skill = project / ".github/skills/test-skill/SKILL.md"
    world.forget_receipts()
    if legacy == "edited":
        legacy_skill.write_text(legacy_skill.read_text(encoding="utf-8") + _EDIT, encoding="utf-8")
    before = legacy_skill.read_bytes()

    result = world.install(*command)

    output = _output(result)
    assert (project / ".agents/skills/test-skill/SKILL.md").is_file()
    if legacy == "unedited":
        assert result.returncode == 0, output
        assert not legacy_skill.exists()
        assert ".github/skills/" not in world.lock_path.read_text(encoding="utf-8")
    else:
        assert result.returncode == 1, output
        assert legacy_skill.read_bytes() == before
        assert _kept_line(".github/skills/test-skill/SKILL.md", user_scope=False) in output
        assert ".github/skills/test-skill/SKILL.md" in world.lock_path.read_text(encoding="utf-8")
