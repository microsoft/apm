"""Unit contracts for receipt-less skill tree adoption (#3391)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from apm_cli.integration.skill_adoption import adoptable_skill_members, adoption_baselines
from apm_cli.utils.content_hash import compute_file_hash


def _walk(source: Path):
    yield from os.walk(source, followlinks=False)


def _no_ignore(_directory: str, _contents: list[str]) -> list[str]:
    return []


def _no_links(content: str, *_args, **_kwargs) -> tuple[str, int]:
    return content, 0


def _rewrite_links(content: str, *_args, **_kwargs) -> tuple[str, int]:
    rewritten = content.replace("](sibling.md)", "](rendered/sibling.md)")
    return rewritten, int(rewritten != content)


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: s\n---\nSee [x](sibling.md).\n", encoding="ascii")
    (source / "references/guide.md").write_text("Guide.\n", encoding="ascii")
    destination = tmp_path / "deployed/s"
    shutil.copytree(source, destination)
    return source, destination


def _members(source: Path, destination: Path, **overrides) -> frozenset[Path]:
    options = {"walk_source": _walk, "ignore": _no_ignore, "resolve_links": _no_links}
    options.update(overrides)
    return adoptable_skill_members(source, destination, **options)


def test_identical_tree_is_adoptable(tmp_path: Path) -> None:
    source, destination = _tree(tmp_path)
    assert _members(source, destination) == {
        destination / "SKILL.md",
        destination / "references/guide.md",
    }


@pytest.mark.parametrize("change", ["extra", "missing", "edited"])
def test_any_tree_difference_blocks_adoption(tmp_path: Path, change: str) -> None:
    source, destination = _tree(tmp_path)
    if change == "extra":
        (destination / "retired.md").write_text("Old.\n", encoding="ascii")
    elif change == "missing":
        (destination / "references/guide.md").unlink()
    else:
        (destination / "references/guide.md").write_text("Edited.\n", encoding="ascii")
    assert _members(source, destination) == frozenset()


def test_ignored_source_members_are_not_expected(tmp_path: Path) -> None:
    source, destination = _tree(tmp_path)
    (source / "apm.yml").write_text("name: s\n", encoding="ascii")

    def ignore(_directory: str, contents: list[str]) -> list[str]:
        return [name for name in contents if name == "apm.yml"]

    assert destination / "SKILL.md" in _members(source, destination, ignore=ignore)


@pytest.mark.windows_compat
def test_comparison_uses_rendered_markdown(tmp_path: Path) -> None:
    source, destination = _tree(tmp_path)
    assert _members(source, destination, resolve_links=_rewrite_links) == frozenset()
    # Rendered members are deployed LF-only (write_text_lf); write_text would emit CRLF on Windows.
    (destination / "SKILL.md").write_bytes(b"---\nname: s\n---\nSee [x](rendered/sibling.md).\n")
    assert destination / "SKILL.md" in _members(source, destination, resolve_links=_rewrite_links)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_destination_symlink_blocks_adoption(tmp_path: Path) -> None:
    source, destination = _tree(tmp_path)
    (destination / "references/guide.md").unlink()
    (destination / "references/guide.md").symlink_to(source / "references/guide.md")
    assert _members(source, destination) == frozenset()


def test_missing_destination_is_not_adoptable(tmp_path: Path) -> None:
    source, destination = _tree(tmp_path)
    shutil.rmtree(destination)
    assert _members(source, destination) == frozenset()


def test_baselines_require_every_member_to_be_claimed(tmp_path: Path) -> None:
    source, destination = _tree(tmp_path)
    options = {"walk_source": _walk, "ignore": _no_ignore, "resolve_links": _no_links}
    anchor = destination / "SKILL.md"
    assert (
        adoption_baselines(source, destination, owned=lambda path: path == anchor, **options) == {}
    )
    baselines = adoption_baselines(source, destination, owned=lambda _path: True, **options)
    assert baselines == {path: compute_file_hash(path) for path in _members(source, destination)}


def _locked_update(tmp_path: Path):
    """Return an integrator, plan and paths for a v1 -> v2 skill update (#3625)."""
    from types import SimpleNamespace

    from apm_cli.install.deployable_source_plan import DeployableSourcePlan
    from apm_cli.integration.skill_adoption import LockedSkillSources
    from apm_cli.integration.skill_integrator import SkillIntegrator
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType

    def package(name: str, body: bytes, *extra: str) -> PackageInfo:
        root = tmp_path / name
        skill = root / ".apm/skills/alpha"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(b"---\nname: alpha\n---\n" + body)
        for file_name in extra:
            (skill / file_name).write_bytes(b"Added.\n")
        return PackageInfo(
            package=APMPackage(name="pkg", version="1.0.0", package_path=root),
            install_path=root.resolve(),
            package_type=PackageType.APM_PACKAGE,
        )

    new = package("new", b"v2\n", "added.md")
    old = package("old", b"v1\n")
    destination = tmp_path / "deployed/alpha"
    shutil.copytree(old.install_path / ".apm/skills/alpha", destination)
    targets = [KNOWN_TARGETS["copilot"]]
    plan = DeployableSourcePlan.create(
        new, targets, skill_subset=None, hooks_approved=False, canvas_approved=False, skip_bin=True
    )
    fetched: list[str] = []

    def fetch(owner: str, _scratch: Path) -> PackageInfo:
        fetched.append(owner)
        return old

    integrator = SkillIntegrator()
    integrator._materialization_session = SimpleNamespace(
        has_receipt=lambda _path: False, locked_sources=LockedSkillSources(fetch)
    )
    return SimpleNamespace(
        integrator=integrator,
        plan=plan,
        targets=targets,
        source=new.install_path / ".apm/skills/alpha",
        destination=destination,
        fetched=fetched,
    )


def _locked_baselines(update, **overrides) -> dict[Path, str]:
    from apm_cli.integration.skill_adoption import skill_tree_baselines
    from apm_cli.integration.skill_integrator import _build_deployable_copy_ignore

    options = {
        "ignore": _build_deployable_copy_ignore(skip_bin=True, source_plan=update.plan),
        "owned": lambda _path: True,
        "force": False,
        "plan": update.plan,
        "owner": "org/pkg",
        "targets": update.targets,
    }
    options.update(overrides)
    return skill_tree_baselines(update.integrator, update.source, update.destination, **options)


@pytest.mark.windows_compat
def test_update_adopts_tree_identical_to_locked_version(tmp_path: Path) -> None:
    update = _locked_update(tmp_path)
    baselines = _locked_baselines(update)
    anchor = update.destination / "SKILL.md"
    assert baselines == {anchor: compute_file_hash(anchor)}
    assert _locked_baselines(update) == baselines
    assert update.fetched == ["org/pkg"]


@pytest.mark.windows_compat
@pytest.mark.parametrize("change", ["edited", "extra", "unclaimed", "receipt", "force"])
def test_update_never_adopts_a_differing_or_owned_elsewhere_tree(
    tmp_path: Path, change: str
) -> None:
    update = _locked_update(tmp_path)
    overrides: dict = {}
    if change == "edited":
        (update.destination / "SKILL.md").write_bytes(b"Local edits.\n")
    elif change == "extra":
        (update.destination / "personal.md").write_bytes(b"Mine.\n")
    elif change == "unclaimed":
        overrides["owned"] = lambda _path: False
    elif change == "receipt":
        update.integrator._materialization_session.has_receipt = lambda _path: True
    else:
        overrides["force"] = True
    assert _locked_baselines(update, **overrides) == {}


@pytest.mark.windows_compat
def test_update_without_locked_source_keeps_refusal(tmp_path: Path) -> None:
    from apm_cli.integration.skill_adoption import LockedSkillSources

    update = _locked_update(tmp_path)

    def unavailable(_owner: str, _scratch: Path):
        raise RuntimeError("locked commit no longer reachable")

    update.integrator._materialization_session.locked_sources = LockedSkillSources(unavailable)
    assert _locked_baselines(update) == {}
    update.integrator._materialization_session.locked_sources = None
    assert _locked_baselines(update) == {}


def test_locked_sources_close_removes_private_scratch(tmp_path: Path) -> None:
    from apm_cli.integration.skill_adoption import LockedSkillSources

    update = _locked_update(tmp_path)
    scratch: list[Path] = []

    def record(_owner: str, destination: Path):
        scratch.append(destination.parent)

    sources = LockedSkillSources(record)
    assert sources.plan("org/pkg", update.plan, update.targets) is None
    assert scratch and scratch[0].is_dir()
    sources.close()
    assert not scratch[0].exists()
