"""Installed-CLI contracts for ``apm update`` without deployment receipts (#3625).

A fresh clone on a machine without local deployment receipts (CI runner,
apm-action scheduled update) commits the output of a package locked at
v0.4.1. Upstream v0.5.0 changes two skills and adds a file. ``apm update``
must adopt the committed files that are byte-identical to the materialized
v0.4.1 source, replace them with v0.5.0 and move the lockfile. A locally
edited file keeps the #3372 fail-closed refusal, and a failed update leaves
the working tree and lockfile exactly as before.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.file_ops import robust_rmtree
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.windows_compat,
]

_REFUSAL = "no trustworthy local deployment baseline"
_GUIDANCE = "Move the conflicting file aside and retry"
_PACKAGE_URL = "https://github.com/test/skills-package.git"
_SHOW_ME_URL = "https://github.com/test/show-me.git"
_SKILL_ROOTS = (".github/skills", ".opencode/skills", ".agents/skills")


@dataclass(frozen=True)
class _Update:
    isolated: IsolatedApmEnvironment
    env: dict[str, str]
    runner: ApmLifecycleRunner
    clone: Path


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=cwd,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=60,
    ).stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("ascii"))


def _skill(name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {name} skill\n---\n{body}\n"


def _package_v041(root: Path, show_me_sha: str) -> None:
    root.mkdir(parents=True)
    dump_yaml(
        {
            "name": "skills-package",
            "version": "0.4.1",
            "description": "Skills package",
            "dependencies": {"apm": [{"git": _SHOW_ME_URL, "ref": show_me_sha}]},
        },
        root / "apm.yml",
    )
    _write(root / ".apm/skills/alpha/SKILL.md", _skill("alpha", "Alpha v0.4.1."))
    _write(root / ".apm/skills/alpha/notes.md", "Alpha notes v0.4.1.\n")
    _write(root / ".apm/skills/beta/SKILL.md", _skill("beta", "Beta v0.4.1."))
    _write(root / ".apm/skills/gamma/SKILL.md", _skill("gamma", "Gamma stays."))
    _write(root / ".apm/prompts/hello.prompt.md", "---\ndescription: Hello\n---\nHello.\n")
    _write(
        root / ".apm/instructions/style.instructions.md",
        "---\napplyTo: '**'\n---\n# Style\n",
    )


def _package_v050(root: Path) -> None:
    """Change files in two skills and add one file."""
    manifest = load_yaml(root / "apm.yml")
    manifest["version"] = "0.5.0"
    dump_yaml(manifest, root / "apm.yml")
    _write(root / ".apm/skills/alpha/SKILL.md", _skill("alpha", "Alpha v0.5.0."))
    _write(root / ".apm/skills/alpha/notes.md", "Alpha notes v0.5.0.\n")
    _write(root / ".apm/skills/beta/SKILL.md", _skill("beta", "Beta v0.5.0."))
    _write(root / ".apm/skills/beta/added.md", "Beta added in v0.5.0.\n")


def _evict_machine_state(isolated: IsolatedApmEnvironment) -> None:
    """Emulate a fresh CI runner: no receipts and no download cache."""
    # Git packs are read-only on Windows; robust_rmtree clears the attribute.
    for receipts in (isolated.home / ".apm").glob("deployment-receipts*"):
        robust_rmtree(receipts)
    if isolated.cache_root.exists():
        robust_rmtree(isolated.cache_root)
    isolated.cache_root.mkdir(parents=True)


def _receipt_less_update(tmp_path: Path, apm_binary_path: Path) -> _Update:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)

    show_me_source = isolated.package_root / "show-me"
    _write(show_me_source / "SKILL.md", _skill("show-me", "See references/guide.md."))
    _write(show_me_source / "references/guide.md", "Guide.\n")
    show_me = repositories.create("show-me", source_tree=show_me_source)
    show_me_commit = repositories.commit(show_me, message="publish show-me")

    package_source = isolated.package_root / "skills-package"
    _package_v041(package_source, show_me_commit.sha)
    package = repositories.create("skills-package", source_tree=package_source)
    old = repositories.commit(package, message="release v0.4.1")
    repositories.tag(package, "v0.4.1", old)
    env = repositories.url_rewrite_subprocess_env_many(
        ((package, _PACKAGE_URL), (show_me, _SHOW_ME_URL))
    )

    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["copilot", "opencode"],
            "dependencies": {"apm": ["test/skills-package#>=0.1.0"], "mcp": []},
        },
        project / "apm.yml",
    )
    _write(project / ".gitignore", "apm_modules/\n")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    first = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    lock = (project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert "v0.4.1" in lock and "package_type: claude_skill" in lock, lock
    _git(project, env, "init", "--initial-branch=main")
    _git(project, env, "config", "user.email", "test@example.com")
    _git(project, env, "config", "user.name", "APM Test")
    _git(project, env, "config", "core.autocrlf", "false")
    _git(project, env, "add", "--all")
    _git(project, env, "commit", "-m", "commit v0.4.1 output")

    _package_v050(package.worktree)
    new = repositories.commit(package, message="release v0.5.0")
    repositories.tag(package, "v0.5.0", new)

    clone = isolated.work_root / "fresh-clone"
    _git(
        isolated.work_root,
        env,
        "clone",
        "--quiet",
        "-c",
        "core.autocrlf=false",
        str(project),
        str(clone),
    )
    _evict_machine_state(isolated)
    return _Update(isolated, env, runner, clone)


def _deployed(clone: Path, relative: str) -> list[Path]:
    return [clone / root / relative for root in _SKILL_ROOTS if (clone / root).is_dir()]


def _locked_tag(clone: Path) -> str:
    lock = load_yaml(clone / "apm.lock.yaml")
    entry = next(
        dep for dep in lock["dependencies"] if "skills-package" in str(dep.get("repo_url", ""))
    )
    return str(entry.get("resolved_tag") or entry.get("resolved_ref"))


def test_receipt_less_update_replaces_unchanged_locked_files(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Committed v0.4.1 output is adopted and replaced by v0.5.0 (#3625)."""
    update = _receipt_less_update(tmp_path, apm_binary_path)
    clone = update.clone
    alpha = _deployed(clone, "alpha/SKILL.md")
    assert alpha, sorted(p.as_posix() for p in clone.rglob("SKILL.md"))
    show_me_before = {
        path: path.read_bytes() for path in _deployed(clone, "show-me/references/guide.md")
    }
    assert show_me_before

    result = update.runner.run(("update", "--yes"), cwd=clone, env=update.env)

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert _REFUSAL not in " ".join(output.split())
    assert _locked_tag(clone) == "v0.5.0"
    for path in alpha:
        assert "Alpha v0.5.0." in path.read_text(encoding="utf-8")
    for path in _deployed(clone, "alpha/notes.md"):
        assert path.read_text(encoding="utf-8") == "Alpha notes v0.5.0.\n"
    for path in _deployed(clone, "beta/SKILL.md"):
        assert "Beta v0.5.0." in path.read_text(encoding="utf-8")
    added = _deployed(clone, "beta/added.md")
    assert added and all(path.is_file() for path in added)
    for path, content in show_me_before.items():
        assert path.read_bytes() == content
    status = _git(clone, update.env, "status", "--porcelain")
    assert not [line for line in status.splitlines() if line.startswith(" D")], status

    again = update.runner.run(("install", "--no-policy"), cwd=clone, env=update.env)
    assert again.returncode == 0, again.stdout + again.stderr
    assert _git(clone, update.env, "status", "--porcelain") == status


def test_receipt_less_update_with_edited_file_fails_closed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A locally edited file is never replaced and a failed update leaves no trace."""
    update = _receipt_less_update(tmp_path, apm_binary_path)
    clone = update.clone
    edited = _deployed(clone, "alpha/SKILL.md")[0]
    edited.write_bytes(b"Local edits.\n")
    lock_before = (clone / "apm.lock.yaml").read_bytes()
    status_before = _git(clone, update.env, "status", "--porcelain", "--untracked-files=all")

    result = update.runner.run(("update", "--yes"), cwd=clone, env=update.env)

    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode != 0, output
    assert _REFUSAL in output, output
    assert _GUIDANCE in output, output
    assert edited.read_bytes() == b"Local edits.\n"
    assert (clone / "apm.lock.yaml").read_bytes() == lock_before
    status_after = _git(clone, update.env, "status", "--porcelain", "--untracked-files=all")
    assert status_after == status_before, status_after
