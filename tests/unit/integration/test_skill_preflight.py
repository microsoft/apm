"""Read-only preflight shares deployed source selection and exact-path ownership."""

from pathlib import Path

import pytest

from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.skill_preflight import SkillConflict, format_skill_conflicts
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType
from apm_cli.models.dependency import DependencyReference

pytestmark = pytest.mark.component


def test_multiple_conflicts_keep_each_source_and_one_nondestructive_remedy() -> None:
    conflicts = [
        SkillConflict("review", "first", Path("first/SKILL.md"), ".claude/review", ("claude",)),
        SkillConflict("lint", "second", Path("second/SKILL.md"), ".agents/lint", ("codex",)),
    ]
    message = format_skill_conflicts(conflicts)
    for conflict in conflicts:
        assert conflict.message() in message
    assert message.count("Rename the local skill") == 1
    assert "skills: or targets: selection in apm.yml" in message
    assert "--force" not in message
    assert message.isascii()


def _package(tmp_path: Path, layout: str = "subskills") -> PackageInfo:
    root = tmp_path / "package" / "review"
    source = (
        root
        if layout == "native"
        else root / (".apm" if layout == "subskills" else "") / "skills" / "review"
    )
    source.mkdir(parents=True)
    (source / "SKILL.md").write_bytes(
        b"---\nname: review\ndescription: Review\n---\nPackage review.\n"
    )
    return PackageInfo(
        package=APMPackage(name="review", version="1.0.0", package_path=root),
        install_path=root,
        dependency_ref=DependencyReference.parse("../review"),
        package_type=PackageType.CLAUDE_SKILL if layout == "native" else PackageType.APM_PACKAGE,
    )


def _conflicts(
    info: PackageInfo,
    root: Path,
    managed: set[str] | None = None,
    *,
    subset: tuple[str, ...] | None = None,
    force: bool = False,
    reverse: bool = False,
) -> tuple:
    targets = [KNOWN_TARGETS[name] for name in ("claude", "copilot", "codex")]
    if reverse:
        targets.reverse()
    plan = DeployableSourcePlan.create(
        info,
        targets,
        skill_subset=subset,
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
    )
    return SkillIntegrator().find_unmanaged_conflicts(
        info,
        root,
        targets=targets,
        source_plan=plan,
        managed_files=managed,
        force=force,
        skip_bin=True,
    )


@pytest.mark.parametrize("layout", ["native", "subskills"])
@pytest.mark.parametrize("reverse", [False, True])
def test_exact_file_conflict_in_every_target_order(
    tmp_path: Path, layout: str, reverse: bool
) -> None:
    info = _package(tmp_path, layout)
    root = tmp_path / "consumer"
    target = root / ".claude/skills/review/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"Local review")
    conflicts = _conflicts(info, root, {".agents/skills/review/SKILL.md"}, reverse=reverse)
    assert len(conflicts) == 1
    assert conflicts[0].path == ".claude/skills/review/SKILL.md"
    assert set(conflicts[0].targets) == {"claude", "copilot", "codex"}
    assert target.read_bytes() == b"Local review"
    assert not (root / ".agents").exists()


def test_same_directory_name_does_not_establish_a_conflict_or_ownership(tmp_path: Path) -> None:
    info = _package(tmp_path)
    root = tmp_path / "consumer"
    target = root / ".claude/skills/review"
    target.mkdir(parents=True)
    (target / "personal.txt").write_bytes(b"Unrelated asset")
    assert _conflicts(info, root) == ()
    (target / "SKILL.md").write_bytes(b"Different unmanaged file")
    assert len(_conflicts(info, root, {".claude/skills/review"})) == 1
    assert (target / "personal.txt").read_bytes() == b"Unrelated asset"


@pytest.mark.parametrize("resolution", ["identical", "managed", "force", "narrowed"])
def test_preserve_existing_nonconflicting_cases(tmp_path: Path, resolution: str) -> None:
    info = _package(tmp_path)
    root = tmp_path / "consumer"
    target = root / ".claude/skills/review/SKILL.md"
    target.parent.mkdir(parents=True)
    source = info.install_path / ".apm/skills/review/SKILL.md"
    content = source.read_bytes() if resolution == "identical" else b"Old content"
    target.write_bytes(content)
    managed = {".claude/skills/review/SKILL.md"} if resolution == "managed" else set()
    assert (
        _conflicts(
            info,
            root,
            managed,
            subset=("lint",) if resolution == "narrowed" else None,
            force=resolution == "force",
        )
        == ()
    )
    assert target.read_bytes() == content


def test_shared_physical_destination_reports_one_conflict(tmp_path: Path) -> None:
    info = _package(tmp_path)
    root = tmp_path / "consumer"
    target = root / ".agents/skills/review/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"Local shared skill")
    assert [conflict.path for conflict in _conflicts(info, root)] == [
        ".agents/skills/review/SKILL.md"
    ]


def test_preflight_uses_rendered_bytes_without_writing(tmp_path: Path, monkeypatch) -> None:
    info = _package(tmp_path)
    root = tmp_path / "consumer"
    target = root / ".claude/skills/review/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"Rendered link.\n")
    observed = []

    def rewrite(self, content, source, destination, *, preserved_source_root=None):
        observed.append((source, destination, preserved_source_root))
        return "Rendered link.\n", 1

    monkeypatch.setattr(SkillIntegrator, "resolve_links", rewrite)
    assert _conflicts(info, root) == ()
    assert observed == [
        (
            info.install_path / ".apm/skills/review/SKILL.md",
            target,
            info.install_path / ".apm/skills/review",
        )
    ]
    assert target.read_bytes() == b"Rendered link.\n"


@pytest.mark.parametrize(
    ("kind", "layout", "route"),
    [
        (PackageType.APM_PACKAGE, "native", "subskills"),
        (PackageType.CLAUDE_SKILL, "native", "native"),
        (PackageType.HYBRID, "subskills", "subskills"),
        (PackageType.SKILL_BUNDLE, "bundle", "bundle"),
    ],
)
def test_source_route_keeps_public_type_boundaries(
    tmp_path: Path,
    kind: PackageType,
    layout: str,
    route: str,
) -> None:
    info = _package(tmp_path, layout)
    info.package_type = kind
    selection = SkillIntegrator.selected_skill_sources(info)
    assert selection.route == route
    if route == "native":
        assert selection.native_root == info.install_path
    elif layout != "native":
        assert set(selection.children) == {"review"}


def test_copy_excluded_nested_bin_is_not_a_conflict(tmp_path: Path) -> None:
    info = _package(tmp_path, "native")
    source = info.install_path / "nested/bin/script.sh"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"Source script\n")
    root = tmp_path / "consumer"
    local = root / ".claude/skills/review/nested/bin/script.sh"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"Unrelated local script\n")
    assert _conflicts(info, root) == ()
    assert local.read_bytes() == b"Unrelated local script\n"
