"""Real-CLI lifecycle: a pull that drops a package no longer orphans its files (#3188).

A teammate's commit removes ``beta`` from both ``apm.yml`` and
``apm.lock.yaml``. No lock entry names beta's deployed skill any more, but
this installation's deployment receipt still does. A complete, successful,
full install or prune removes the receipted file when it still holds the
receipted bytes; ``apm audit --ci`` reports it read-only. Everything without
that evidence (an edit, no receipt, another owner's claim, a moved checkout,
a failed or narrowed install) keeps its files and receipts (req-lk-024).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-deployment-provenance-state"),
]

_BETA = ".claude/skills/beta/SKILL.md"
_ALPHA = ".claude/skills/alpha/SKILL.md"
_SIBLING = ".claude/skills/beta/NOTES.md"
_EXTRA = ".claude/skills/beta-extra/SKILL.md"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _manifest(*packages: str) -> str:
    lines = "".join(f"    - ./pkgs/{name}\n" for name in packages)
    return f"name: consumer\nversion: 0.1.0\ntarget: claude\ndependencies:\n  apm:\n{lines}"


def _package(project: Path, name: str, *skills: str) -> None:
    _write(project / "pkgs" / name / "apm.yml", f"name: {name}\nversion: 0.1.0\n")
    for skill in skills:
        _write(
            project / "pkgs" / name / ".apm" / "skills" / skill / "SKILL.md",
            f"---\nname: {skill}\ndescription: {skill} skill\n---\n{skill.title()}.\n",
        )


def _evidence(result: CommandResult) -> str:
    return f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"


class _World:
    """One consumer project that installed alpha and beta, before the pull."""

    def __init__(
        self, tmp_path: Path, apm_binary_path: Path, *packages: str, beta=("beta",)
    ) -> None:
        isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
        self.env = isolated.subprocess_env()
        # A developer workstation: a truthy CI would make install --frozen.
        self.env.pop("CI", None)
        self.receipts_dir = isolated.config_root / "deployment-receipts"
        self.root = isolated.work_root / "consumer"
        self._runner = ApmLifecycleRunner((str(apm_binary_path),))
        _package(self.root, "alpha", "alpha")
        _package(self.root, "beta", *beta)
        _write(self.root / "apm.yml", _manifest(*packages))

    def run(self, *args: str, cwd: Path | None = None, ok: bool = True) -> CommandResult:
        result = self._runner.run(args, scenario_id="3188", cwd=cwd or self.root, env=self.env)
        if ok:
            assert result.returncode == 0, _evidence(result)
        return result

    def pull(self, root: Path | None = None) -> None:
        """Rewrite apm.yml and apm.lock.yaml as a teammate's commit without beta does."""
        root = root or self.root
        _write(root / "apm.yml", _manifest("alpha"))
        lock = yaml.safe_load((root / "apm.lock.yaml").read_text(encoding="utf-8"))
        lock["dependencies"] = [dep for dep in lock["dependencies"] if dep["name"] != "beta"]
        lock["deployments"] = [
            row for row in lock["deployments"] if "./pkgs/beta" not in row["owners"]
        ]
        _write(root / "apm.lock.yaml", yaml.safe_dump(lock, sort_keys=False))

    def destinations(self, root: Path | None = None) -> set[str]:
        """Receipted paths, relative to *root*, of the receipt recorded for *root*."""
        root = (root or self.root).resolve()
        for path in self.receipts_dir.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["root"] == root.as_posix():
                return {
                    Path(dest).relative_to(root).as_posix()
                    for dest in data["destinations"].values()
                }
        return set()

    def receipts(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in sorted(self.receipts_dir.glob("*.json"))}

    def tree(self, root: Path | None = None) -> dict[str, bytes]:
        root = root or self.root
        return {
            path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else b"<dir>"
            for path in sorted(root.rglob("*"))
        }


def _installed(tmp_path: Path, apm_binary_path: Path, *beta: str) -> _World:
    world = _World(tmp_path, apm_binary_path, "alpha", "beta", beta=beta or ("beta",))
    world.run("install")
    assert world.destinations() == {_ALPHA, _BETA} | ({_EXTRA} if beta else set())
    return world


def test_audit_ci_reports_the_unclaimed_receipted_file_read_only(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _installed(tmp_path, apm_binary_path)
    world.pull()
    tree, receipts = world.tree(), world.receipts()

    result = world.run("audit", "--ci", ok=False)

    assert result.returncode != 0, _evidence(result)
    assert f"orphaned: {_BETA}" in result.stdout + result.stderr, _evidence(result)
    assert world.tree() == tree
    assert world.receipts() == receipts


@pytest.mark.parametrize("sibling", [True, False], ids=["unreceipted-sibling", "alone"])
def test_install_after_pull_removes_the_receipted_file_and_is_stable(
    tmp_path: Path, apm_binary_path: Path, sibling: bool
) -> None:
    world = _installed(tmp_path, apm_binary_path)
    if sibling:
        _write(world.root / _SIBLING, "My notes.\n")
    alpha = (world.root / _ALPHA).read_bytes()
    world.pull()
    lock = (world.root / "apm.lock.yaml").read_bytes()

    result = world.run("install")

    assert _BETA in result.stdout, _evidence(result)
    assert not (world.root / _BETA).exists()
    assert (world.root / _ALPHA).read_bytes() == alpha
    assert (world.root / ".claude/skills/beta").is_dir() is sibling
    assert (world.root / ".claude/skills/alpha").is_dir()
    if sibling:
        assert (world.root / _SIBLING).read_text(encoding="utf-8") == "My notes.\n"
    assert world.destinations() == {_ALPHA}
    assert (world.root / "apm.lock.yaml").read_bytes() == lock

    tree, receipts = world.tree(), world.receipts()
    world.run("install")
    assert world.tree() == tree
    assert world.receipts() == receipts
    if not sibling:
        world.run("audit", "--ci")


@pytest.mark.parametrize("checkout", [True, False], ids=["with-checkout", "checkout-gone"])
def test_prune_alone_removes_the_receipted_file(
    tmp_path: Path, apm_binary_path: Path, checkout: bool
) -> None:
    world = _installed(tmp_path, apm_binary_path)
    _write(world.root / _SIBLING, "My notes.\n")
    world.pull()
    if not checkout:
        shutil.rmtree(world.root / "apm_modules" / "_local" / "beta")

    result = world.run("prune")

    assert _BETA in result.stdout, _evidence(result)
    assert not (world.root / _BETA).exists()
    assert (world.root / _SIBLING).read_text(encoding="utf-8") == "My notes.\n"
    assert (world.root / _ALPHA).is_file()
    assert world.destinations() == {_ALPHA}
    assert not (world.root / "apm_modules" / "_local" / "beta").exists()


def test_edited_unclaimed_file_is_kept_and_named_once(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _installed(tmp_path, apm_binary_path)
    world.pull()
    _write(world.root / _BETA, "My edit.\n")

    result = world.run("install")

    assert (world.root / _BETA).read_text(encoding="utf-8") == "My edit.\n"
    assert world.destinations() == {_ALPHA, _BETA}
    output = result.stdout + result.stderr
    named = [line for line in output.splitlines() if _BETA in line]
    assert len(named) == 1, _evidence(result)
    assert "manually" in output, _evidence(result)


def test_unreceipted_identical_skill_is_kept(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _installed(tmp_path, apm_binary_path)
    beta = (world.root / _BETA).read_bytes()
    copy = world.root / ".claude/skills/beta-copy/SKILL.md"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(beta)
    world.pull()

    world.run("install")

    # Identical bytes without a receipt are no evidence; the receipted beta goes.
    assert copy.read_bytes() == beta
    assert not (world.root / _BETA).exists()
    assert world.destinations() == {_ALPHA}


def test_path_another_dependency_claims_is_kept(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _installed(tmp_path, apm_binary_path, "beta", "beta-extra")
    beta = (world.root / _BETA).read_bytes()
    # The teammate moved one skill into a new package; their lock claims its path.
    teammate = world.root.parent / "teammate"
    shutil.copytree(world.root / "pkgs", teammate / "pkgs")
    shutil.copytree(teammate / "pkgs" / "beta", teammate / "pkgs" / "gamma")
    shutil.rmtree(teammate / "pkgs" / "gamma" / ".apm" / "skills" / "beta-extra")
    _write(teammate / "pkgs" / "gamma" / "apm.yml", "name: gamma\nversion: 0.1.0\n")
    _write(teammate / "apm.yml", _manifest("alpha", "gamma"))
    world.run("install", cwd=teammate)
    shutil.copytree(teammate / "pkgs" / "gamma", world.root / "pkgs" / "gamma")
    for name in ("apm.yml", "apm.lock.yaml"):
        shutil.copyfile(teammate / name, world.root / name)

    world.run("prune")

    assert (world.root / _BETA).read_bytes() == beta
    assert world.destinations() == {_ALPHA, _BETA}
    assert not (world.root / _EXTRA).exists()

    world.run("install")

    assert (world.root / _BETA).read_bytes() == beta
    assert _BETA in world.destinations()


def test_moved_checkout_keeps_files_and_receipts(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _installed(tmp_path, apm_binary_path)
    moved = world.root.parent / "moved"
    shutil.copytree(world.root, moved)
    world.pull(moved)
    receipt = world.receipts()

    world.run("prune", cwd=moved)
    world.run("install", cwd=moved)

    assert (moved / _BETA).is_file()
    assert _BETA not in world.destinations(moved)
    assert {name: data for name, data in world.receipts().items() if name in receipt} == receipt

    # The receipt still binds the original checkout, where the same pull cleans up.
    world.pull()
    world.run("install")
    assert not (world.root / _BETA).exists()


@pytest.mark.parametrize("operation", ["failed-install", "narrowed-install", "only-apm-install"])
def test_failed_or_narrowed_install_keeps_files_and_receipts(
    tmp_path: Path, apm_binary_path: Path, operation: str
) -> None:
    world = _installed(tmp_path, apm_binary_path)
    world.pull()
    beta = (world.root / _BETA).read_bytes()
    if operation == "failed-install":
        _write(world.root / "apm.yml", _manifest("alpha", "missing"))
        result = world.run("install", ok=False)
        assert result.returncode != 0, _evidence(result)
    elif operation == "narrowed-install":
        world.run("install", "./pkgs/alpha")
    else:
        world.run("install", "--only=apm")

    assert (world.root / _BETA).read_bytes() == beta
    assert world.destinations() == {_ALPHA, _BETA}

    # The next complete, successful, full install removes it.
    _write(world.root / "apm.yml", _manifest("alpha"))
    world.run("install")
    assert not (world.root / _BETA).exists()
    assert world.destinations() == {_ALPHA}
