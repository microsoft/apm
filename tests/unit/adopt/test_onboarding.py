"""Observable component proofs for metadata-only onboarding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.adopt.discovery import discover
from apm_cli.commands.init import init
from apm_cli.utils.yaml_io import load_yaml

pytestmark = pytest.mark.component
SKILL = b"---\r\nname: review\r\ndescription: Review code\r\n---\r\n# Review\r\n"


def snapshot(root: Path) -> dict[str, bytes]:
    """Capture all regular file payloads without following symlinks."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate filesystem reads, user metadata and lifecycle locks."""
    root = tmp_path / "project"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(root)
    return root


def skill(root: Path, relative: str = ".claude/skills/review") -> Path:
    """Create an existing skill, without APM source metadata."""
    path = root / relative
    path.mkdir(parents=True)
    (path / "SKILL.md").write_bytes(SKILL)
    (path / "resource.bin").write_bytes(b"\x00\xff\r\nkeep")
    return path


def invoke(*args: str):
    """Invoke the real Click command facade without init's unrelated bootstrap."""
    return CliRunner().invoke(init, ["--discover", *args])


def test_discovery_is_read_only_and_apply_changes_only_consumer(workspace: Path) -> None:
    source = skill(workspace)
    before = snapshot(workspace)
    home = snapshot(Path.home())
    result = invoke("--format", "json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["additions"] == [{"path": "./.claude/skills/review"}]
    assert snapshot(workspace) == before
    assert snapshot(Path.home()) == home
    result = invoke("--apply", "--yes")
    assert result.exit_code == 0, result.output
    assert snapshot(source) == {"SKILL.md": SKILL, "resource.bin": b"\x00\xff\r\nkeep"}
    assert set(snapshot(workspace)) - set(before) == {"apm.yml"}
    assert load_yaml(workspace / "apm.yml")["dependencies"]["apm"] == report["additions"]
    applied = snapshot(workspace)
    result = invoke("--write", "--yes")
    assert result.exit_code == 0, result.output
    assert snapshot(workspace) == applied


def test_apply_without_consent_has_no_writes(workspace: Path) -> None:
    skill(workspace)
    before = snapshot(workspace.parent)
    result = invoke("--apply")
    assert result.exit_code != 0
    assert "consent" in result.output
    assert snapshot(workspace.parent) == before


@pytest.mark.parametrize(
    "entry",
    [
        "    - ./.claude/skills/review",
        "    - path: ./.claude/skills/../skills/review\n      alias: custom\n      targets: [copilot]",
    ],
)
def test_equivalent_references_keep_comments_options_and_bytes(workspace: Path, entry: str) -> None:
    skill(workspace)
    manifest = workspace / "apm.yml"
    manifest.write_text(
        "# User comment\nname: consumer\nversion: 1.0.0\n"
        "scripts:\n  keep: echo yes\ndependencies:\n  apm:\n" + entry + "\n"
    )
    original = manifest.read_bytes()
    result = invoke("--apply", "--yes", "--format", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["findings"][0]["status"] == "already-declared"
    assert manifest.read_bytes() == original


def test_new_dependency_preserves_existing_settings(workspace: Path) -> None:
    skill(workspace)
    manifest = workspace / "apm.yml"
    manifest.write_text(
        "# Keep this comment\nname: consumer\nversion: 1.0.0\n"
        "settings:\n  my_setting: true\n"
        "dependencies:\n  apm:\n    - git: owner/existing\n      targets: [copilot]\n"
    )
    assert invoke("--apply", "--yes").exit_code == 0
    assert manifest.read_bytes().startswith(b"# Keep this comment\n")
    data = load_yaml(manifest)
    assert data["settings"] == {"my_setting": True}
    assert data["dependencies"]["apm"][0] == {"git": "owner/existing", "targets": ["copilot"]}


def test_unsupported_native_content_is_reported_without_values(workspace: Path) -> None:
    (workspace / ".claude").mkdir()
    (workspace / ".claude/settings.json").write_text('{"hooks": {"secret": "DO-NOT-PRINT"}}')
    (workspace / "AGENTS.md").write_text("private instruction text")
    before = snapshot(workspace)
    result = invoke("--apply", "--yes", "--format", "json")
    assert result.exit_code == 0, result.output
    assert "DO-NOT-PRINT" not in result.output
    assert "private instruction text" not in result.output
    assert {item["status"] for item in json.loads(result.output)["findings"]} == {"unsupported"}
    assert snapshot(workspace) == before


@pytest.mark.parametrize("existing", [False, True])
def test_install_identity_collision_refuses_entire_write(workspace: Path, existing: bool) -> None:
    skill(workspace)
    if existing:
        (workspace / "apm.yml").write_text(
            "name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n    - ../other/review\n"
        )
    else:
        skill(workspace, ".github/skills/review")
    before = snapshot(workspace)
    report = discover(workspace, workspace / "apm.yml")
    assert any("collision" in item["reason"] for item in report["findings"])
    assert invoke("--apply", "--yes").exit_code == 1
    assert snapshot(workspace) == before


def test_predeclared_source_collision_refuses_with_exit_one(workspace: Path) -> None:
    """An intended source declaration never grants ownership of a same-name target."""
    skill(workspace)
    skill(workspace, ".agents/skills/review")
    (workspace / "apm.yml").write_text(
        "name: consumer\nversion: 1.0.0\ndependencies:\n"
        "  apm:\n    - path: ./.claude/skills/review\n      targets: [copilot]\n"
    )
    before = snapshot(workspace.parent)
    result = invoke("--apply", "--yes", "--format", "json")
    assert result.exit_code == 1, result.output
    assert "collision" in result.output
    assert snapshot(workspace.parent) == before


@pytest.mark.parametrize("inventoried", [False, True])
@pytest.mark.parametrize("second_group", ["dependencies", "devDependencies"])
def test_existing_slot_collision_blocks_unrelated_addition(
    workspace: Path, inventoried: bool, second_group: str
) -> None:
    """Validate every existing reference, even when discovery would skip it."""
    prefix = ".claude/skills/" if inventoried else ""
    first, second = (f"{prefix}{name}/review" for name in ("a", "b"))
    skill(workspace, first)
    skill(workspace, second)
    skill(workspace, ".claude/skills/unrelated")
    manifest = (
        "# Keep all existing declarations\nname: consumer\nversion: 1.0.0\n"
        f"dependencies:\n  apm:\n    - ./{first}\n"
    )
    if second_group == "devDependencies":
        manifest += "devDependencies:\n  apm:\n"
    manifest += f"    - path: ./{second}\n      targets: [copilot]\n"
    (workspace / "apm.yml").write_text(manifest)
    before = snapshot(workspace.parent)

    preview = invoke("--format", "json")
    assert preview.exit_code == 0, preview.output
    report = json.loads(preview.output)
    assert report["additions"] == [{"path": "./.claude/skills/unrelated"}]
    result = invoke("--apply", "--yes", "--format", "json")
    assert result.exit_code == 1, result.output
    assert "collision" in result.output
    assert any(
        item["status"] == "unsafe" and "collision" in item["reason"] for item in report["findings"]
    )
    assert snapshot(workspace.parent) == before


def test_repeated_equivalent_references_preserve_options_while_adding(workspace: Path) -> None:
    """Equivalent string/object references are not conflicting install slots."""
    source = skill(workspace)
    skill(workspace, ".claude/skills/unrelated")
    manifest = workspace / "apm.yml"
    manifest.write_text(
        "# Preserve repeated declarations and their options\n"
        "name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n"
        "    - ./.claude/skills/review\n"
        f"    - path: {source}\n      alias: custom\n      targets: [copilot]\n"
    )
    original_entries = load_yaml(manifest)["dependencies"]["apm"]
    result = invoke("--apply", "--yes", "--format", "json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert not any(item["status"] == "unsafe" for item in report["findings"])
    assert report["additions"] == [{"path": "./.claude/skills/unrelated"}]
    assert load_yaml(manifest)["dependencies"]["apm"] == [
        *original_entries,
        {"path": "./.claude/skills/unrelated"},
    ]
    assert manifest.read_bytes().startswith(b"# Preserve repeated declarations and their options\n")
    applied = snapshot(workspace.parent)
    assert invoke("--apply", "--yes").exit_code == 0
    assert snapshot(workspace.parent) == applied


@pytest.mark.parametrize("symlink_kind", ["source", "nested", "manifest"])
def test_unsafe_symlinks_refuse_before_write(workspace: Path, symlink_kind: str) -> None:
    source = skill(workspace)
    outside = workspace.parent / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_bytes(SKILL)
    if symlink_kind == "source":
        (workspace / ".claude/skills/evil").symlink_to(outside, target_is_directory=True)
    elif symlink_kind == "nested":
        (source / "escape").symlink_to(outside, target_is_directory=True)
    else:
        (workspace / "apm.yml").symlink_to(outside / "manifest.yml")
    before = snapshot(workspace.parent)
    assert invoke("--apply", "--yes").exit_code != 0
    assert snapshot(workspace.parent) == before


@pytest.mark.parametrize(
    "invalid",
    [
        "[oops]",
        "name: [oops]\nversion: 1.0.0",
        "name: x\nversion: 1.0.0\ndependencies:\n  apm: invalid",
    ],
)
def test_invalid_manifest_fails_visibly_without_replacing(workspace: Path, invalid: str) -> None:
    skill(workspace)
    (workspace / "apm.yml").write_text(invalid)
    before = snapshot(workspace)
    result = invoke("--apply", "--yes")
    assert result.exit_code != 0
    assert "invalid metadata" in result.output
    assert snapshot(workspace) == before


def test_global_uses_existing_user_manifest_and_absolute_refs(workspace: Path) -> None:
    source = skill(Path.home())
    before = snapshot(source)
    assert invoke("--global", "--format", "yaml").exit_code == 0
    assert not (Path.home() / ".apm").exists()
    result = invoke("--global", "--apply", "--yes")
    assert result.exit_code == 0, result.output
    manifest = Path.home() / ".apm/apm.yml"
    assert load_yaml(manifest)["dependencies"]["apm"] == [{"path": str(source)}]
    assert not (workspace / "apm.yml").exists()
    assert snapshot(source) == before
    original = manifest.read_bytes()
    assert invoke("--global", "--apply", "--yes").exit_code == 0
    assert manifest.read_bytes() == original


def test_self_dependency_is_refused(workspace: Path) -> None:
    (workspace / "SKILL.md").write_bytes(SKILL)
    assert invoke("--apply", "--yes").exit_code != 0
    assert not (workspace / "apm.yml").exists()


def test_plain_init_still_scaffolds(workspace: Path) -> None:
    result = CliRunner().invoke(init, ["--yes", "--target", "copilot"])
    assert result.exit_code == 0, result.output
    assert (workspace / "apm.yml").exists()


def test_generated_skill_is_not_rediscovered(workspace: Path) -> None:
    from apm_cli.deps.lockfile import LockFile

    skill(workspace)
    skill(workspace, ".agents/skills/review")
    lock = LockFile(local_deployed_files=[".agents/skills/review"])
    lock.write(workspace / "apm.lock.yaml")
    before = snapshot(workspace)
    report = discover(workspace, workspace / "apm.yml")
    assert report["additions"] == [{"path": "./.claude/skills/review"}]
    assert any(item["status"] == "managed" for item in report["findings"])
    assert snapshot(workspace) == before


def test_full_cli_discovery_never_checks_updates(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.cli import cli

    skill(workspace)

    def forbidden() -> None:
        raise AssertionError("Discovery must not contact the update service")

    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", forbidden)
    for args in (["init", "--discover"], ["discover"]):
        result = CliRunner().invoke(cli, [*args, "--format", "json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["additions"]


def test_changed_candidate_is_revalidated_before_apply(workspace: Path) -> None:
    from apm_cli.adopt.materialize import apply_report

    source = skill(workspace)
    report = discover(workspace, workspace / "apm.yml")
    (source / "unsafe").symlink_to(workspace.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="changed"):
        apply_report(report)
    assert not (workspace / "apm.yml").exists()


def test_supported_existing_package_root(workspace: Path) -> None:
    package = workspace / ".claude/skills/package"
    (package / ".apm").mkdir(parents=True)
    (package / "apm.yml").write_text("name: package\nversion: 1.0.0\n")
    original = snapshot(package)
    result = invoke("--apply", "--yes")
    assert result.exit_code == 0, result.output
    assert snapshot(package) == original
    assert load_yaml(workspace / "apm.yml")["dependencies"]["apm"] == [
        {"path": "./.claude/skills/package"}
    ]


def test_read_only_admission_never_normalizes_a_plugin(workspace: Path) -> None:
    """The existing admission owner must refuse a source-mutating format path."""
    from apm_cli.models.validation import validate_apm_package

    package = workspace / "plugin"
    (package / ".claude-plugin").mkdir(parents=True)
    (package / ".claude-plugin/plugin.json").write_text('{"name": "plugin", "version": "1.0.0"}')
    before = snapshot(workspace)
    result = validate_apm_package(package, read_only=True)
    assert not result.is_valid
    assert snapshot(workspace) == before


def test_non_directory_apm_marker_is_not_admitted(workspace: Path) -> None:
    package = workspace / ".claude/skills/not-a-package"
    package.mkdir(parents=True)
    (package / "apm.yml").write_text("name: bad\nversion: 1.0.0\n")
    (package / ".apm").write_text("not a directory")
    result = invoke("--apply", "--yes", "--format", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["findings"][0]["status"] == "unsupported"
    assert not (workspace / "apm.yml").exists()


@pytest.mark.parametrize("accepted", [False, True])
def test_interactive_consent_is_explicit(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, accepted: bool
) -> None:
    import sys
    from types import SimpleNamespace

    import click

    from apm_cli.commands.discover import run_discover

    skill(workspace)
    before = snapshot(workspace.parent)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    confirmations = []

    def confirm(message: str, *, default: bool) -> bool:
        confirmations.append((message, default))
        return accepted

    monkeypatch.setattr(click, "confirm", confirm)
    if accepted:
        run_discover(write=True, output_format="text", global_=False, yes=False, verbose=False)
        assert (workspace / "apm.yml").exists()
    else:
        with pytest.raises(click.Abort):
            run_discover(write=True, output_format="text", global_=False, yes=False, verbose=False)
        assert snapshot(workspace.parent) == before
    assert len(confirmations) == 1
    assert confirmations[0][1] is False


def test_global_home_reference_deduplicates(workspace: Path) -> None:
    skill(Path.home())
    metadata = Path.home() / ".apm"
    metadata.mkdir()
    manifest = metadata / "apm.yml"
    manifest.write_text(
        "name: user\nversion: 1.0.0\ndependencies:\n"
        "  apm:\n    - path: ~/.claude/skills/review\n      targets: [copilot]\n"
    )
    original = manifest.read_bytes()
    result = invoke("--global", "--apply", "--yes")
    assert result.exit_code == 0, result.output
    assert manifest.read_bytes() == original


def test_bounded_source_and_non_regular_paths_are_rejected(workspace: Path) -> None:
    from apm_cli.adopt.safety import MAX_FILE_BYTES

    source = skill(workspace)
    (source / "large.bin").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    before = snapshot(source)
    assert invoke("--apply", "--yes").exit_code != 0
    assert not (workspace / "apm.yml").exists()
    assert snapshot(source) == before
