"""Installed-CLI regressions for global Claude native-rule deduplication."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.compilation.constants import AGENTS_MD_GENERATED_MARKER
from apm_cli.utils.yaml_io import dump_yaml_roundtrip, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_STYLE = "GLOBAL-STYLE-SENTINEL"
_WORKFLOW = "GLOBAL-WORKFLOW-SENTINEL"


@dataclass
class _Lifecycle:
    """Keep each real-CLI lifecycle's roots and process inputs together."""

    isolated: IsolatedApmEnvironment
    environment: dict[str, str]
    runner: ApmLifecycleRunner
    claude_root: Path
    package: Path

    @property
    def memory(self) -> Path:
        """Return the selected global Claude memory path."""
        return self.claude_root / "CLAUDE.md"

    @property
    def rules(self) -> Path:
        """Return the selected global Claude rule directory."""
        return self.claude_root / "rules"

    @property
    def codex(self) -> Path:
        """Return the independent Codex global output."""
        return self.isolated.home / ".codex" / "AGENTS.md"

    def run(
        self, *arguments: str, cwd: Path | None = None, expected_returncode: int = 0
    ) -> CommandResult:
        """Execute a command with its expected status and complete failure evidence."""
        result = self.runner.run(
            arguments,
            scenario_id="global-claude-native-rule-dedup",
            cwd=cwd or self.isolated.work_root,
            env=self.environment,
        )
        assert result.returncode == expected_returncode, f"{result.stdout}\n{result.stderr}"
        return result

    def install(self) -> None:
        """Install only a local package into the isolated user scope."""
        self.run(
            "install",
            "-g",
            str(self.package),
            "--target",
            "claude,codex",
            "--no-policy",
            "--parallel-downloads",
            "0",
        )

    def stale_memory(self) -> bytes:
        """Obtain a genuine generated baseline before native rules are visible."""
        hidden = self.claude_root / "hidden-rules"
        self.rules.rename(hidden)
        try:
            self.run("compile", "-g")
            content = self.memory.read_bytes()
            assert AGENTS_MD_GENERATED_MARKER.encode() in content
            assert b"Build ID:" in content
            assert _STYLE.encode() in content
            assert _WORKFLOW.encode() in content
            return content
        finally:
            hidden.rename(self.rules)


def _create_lifecycle(
    tmp_path: Path, apm_binary_path: Path, *, external: bool = False
) -> _Lifecycle:
    """Install real local instructions without network or user-home access."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "lifecycle", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    environment["APM_NO_CACHE"] = "1"
    environment.pop("CLAUDE_CONFIG_DIR", None)
    claude_root = isolated.home / ".claude"
    if external:
        claude_root = isolated.root / "external-claude"
        environment["CLAUDE_CONFIG_DIR"] = str(claude_root)
    factory = LocalPackageFactory(isolated.package_root)
    package = factory.create("global-rules", targets=("claude", "codex"))
    factory.add_instruction(
        package,
        "style",
        f"---\ndescription: Style conventions\n---\n# Style\n{_STYLE}\n",
    )
    factory.add_instruction(
        package,
        "workflow",
        f"---\ndescription: Workflow conventions\n---\n# Workflow\n{_WORKFLOW}\n",
    )
    lifecycle = _Lifecycle(
        isolated,
        environment,
        ApmLifecycleRunner((str(apm_binary_path),)),
        claude_root,
        package.root,
    )
    lifecycle.install()
    assert _STYLE in (lifecycle.rules / "style.md").read_text(encoding="utf-8")
    assert _WORKFLOW in (lifecycle.rules / "workflow.md").read_text(encoding="utf-8")
    return lifecycle


@pytest.mark.parametrize("external", [False, True], ids=["default-home", "external-config"])
def test_matching_native_rules_avoid_duplicate_memory_and_leave_codex_unchanged(
    tmp_path: Path, apm_binary_path: Path, external: bool
) -> None:
    """Native Claude rules replace only Claude fallback, including on replay."""
    lifecycle = _create_lifecycle(tmp_path, apm_binary_path, external=external)
    native_before = {path.name: path.read_bytes() for path in lifecycle.rules.glob("*.md")}
    roots = {"claude": lifecycle.claude_root, "codex": lifecycle.codex.parent}
    before_dry_run = ArtifactSnapshotSet.capture(roots)

    lifecycle.run("compile", "-g", "--clean", "--dry-run")

    assert ArtifactSnapshotSet.capture(roots) == before_dry_run

    lifecycle.run("compile", "-g")

    assert not lifecycle.memory.exists()
    codex_before = lifecycle.codex.read_bytes()
    assert _STYLE.encode() in codex_before
    assert _WORKFLOW.encode() in codex_before
    lifecycle.install()
    lifecycle.run("compile", "-g")
    assert not lifecycle.memory.exists()
    assert lifecycle.codex.read_bytes() == codex_before
    assert {path.name: path.read_bytes() for path in lifecycle.rules.glob("*.md")} == native_before
    if external:
        assert not (lifecycle.isolated.home / ".claude" / "CLAUDE.md").exists()


@pytest.mark.parametrize("external", [False, True], ids=["default-home", "external-config"])
def test_stale_generated_memory_requires_explicit_clean_and_dry_run_is_read_only(
    tmp_path: Path, apm_binary_path: Path, external: bool
) -> None:
    """Normal compile explains cleanup; only a real clean deletes intact output."""
    lifecycle = _create_lifecycle(tmp_path, apm_binary_path, external=external)
    stale = lifecycle.stale_memory()
    codex_before = lifecycle.codex.read_bytes()
    native_before = {path.name: path.read_bytes() for path in lifecycle.rules.glob("*.md")}

    result = lifecycle.run("compile", "-g")

    assert lifecycle.memory.read_bytes() == stale
    message = " ".join((result.stdout + result.stderr).split())
    assert "CLAUDE.md" in "".join(message.split())
    assert "--clean" in message
    assert "compile" in message
    roots = {
        "claude": lifecycle.claude_root,
        "codex": lifecycle.codex.parent,
        "apm": lifecycle.isolated.config_root,
    }
    before_dry_run = ArtifactSnapshotSet.capture(roots)
    lifecycle.run("compile", "-g", "--clean", "--dry-run")
    assert ArtifactSnapshotSet.capture(roots) == before_dry_run
    assert lifecycle.memory.read_bytes() == stale
    assert lifecycle.codex.read_bytes() == codex_before
    assert {path.name: path.read_bytes() for path in lifecycle.rules.glob("*.md")} == native_before

    cleaned = lifecycle.run("compile", "-g", "--clean")

    assert not lifecycle.memory.exists()
    assert "No user-scope root context files changed." not in " ".join(
        (cleaned.stdout + cleaned.stderr).split()
    )
    assert lifecycle.codex.read_bytes() == codex_before
    assert {path.name: path.read_bytes() for path in lifecycle.rules.glob("*.md")} == native_before
    lifecycle.run("compile", "-g", "--clean")
    lifecycle.install()
    lifecycle.run("compile", "-g")
    assert not lifecycle.memory.exists()
    assert lifecycle.codex.read_bytes() == codex_before


@pytest.mark.parametrize(
    "mutation", ["missing", "unrelated", "changed-content", "scoped", "duplicate-filename"]
)
def test_only_the_individually_matching_instruction_is_suppressed(
    tmp_path: Path, apm_binary_path: Path, mutation: str
) -> None:
    """Unusable style rules must not suppress style or retain matching workflow."""
    lifecycle = _create_lifecycle(tmp_path, apm_binary_path)
    style = lifecycle.rules / "style.md"
    original = style.read_bytes()
    style.unlink()
    if mutation == "unrelated":
        (lifecycle.rules / "unrelated.md").write_bytes(original)
    elif mutation == "changed-content":
        style.write_bytes(original.replace(_STYLE.encode(), b"CHANGED-NATIVE-STYLE"))
    elif mutation == "scoped":
        style.write_bytes(b"---\npaths:\n  - 'src/**/*.py'\n---\n" + original)
    elif mutation == "duplicate-filename":
        duplicate = lifecycle.rules / "nested" / "style.md"
        duplicate.parent.mkdir()
        duplicate.write_bytes(original)

    lifecycle.run("compile", "-g", "--clean")

    fallback = lifecycle.memory.read_text(encoding="utf-8")
    assert _STYLE in fallback
    assert _WORKFLOW not in fallback
    assert "CHANGED-NATIVE-STYLE" not in fallback
    codex = lifecycle.codex.read_text(encoding="utf-8")
    assert _STYLE in codex
    assert _WORKFLOW in codex


def test_no_matching_native_rules_keeps_all_fallback_instructions(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """An unrelated rules directory is not proof of either native instruction."""
    lifecycle = _create_lifecycle(tmp_path, apm_binary_path)
    for path in lifecycle.rules.glob("*.md"):
        path.unlink()
    (lifecycle.rules / "unrelated.md").write_text("# Personal unrelated rule\n", encoding="utf-8")

    lifecycle.run("compile", "-g", "--clean")

    fallback = lifecycle.memory.read_text(encoding="utf-8")
    assert _STYLE in fallback
    assert _WORKFLOW in fallback
    assert "Personal unrelated rule" not in fallback
    lifecycle.run("compile", "-g", "--clean")
    assert lifecycle.memory.read_text(encoding="utf-8") == fallback


def test_canonical_native_content_accepts_platform_newlines(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """CRLF serialization does not turn an equivalent native rule into a fallback."""
    lifecycle = _create_lifecycle(tmp_path, apm_binary_path)
    style = lifecycle.rules / "style.md"
    style.write_bytes(style.read_bytes().replace(b"\n", b"\r\n"))
    native_before = style.read_bytes()

    lifecycle.run("compile", "-g")

    assert not lifecycle.memory.exists()
    assert style.read_bytes() == native_before


@pytest.mark.parametrize(
    ("flags", "hidden", "expected_returncode"),
    [
        ((), "\u202e", 1),
        (("--clean",), "\u202e", 1),
        (("--clean", "--dry-run"), "\u202e", 1),
        ((), "\u200b", 0),
    ],
    ids=["critical", "critical-clean", "critical-preview", "warning-only"],
)
def test_matching_native_rules_cannot_bypass_compiled_output_policy(
    tmp_path: Path,
    apm_binary_path: Path,
    flags: tuple[str, ...],
    hidden: str,
    expected_returncode: int,
) -> None:
    """Native coverage preserves blocking errors and actionable noncritical warnings."""
    lifecycle = _create_lifecycle(tmp_path, apm_binary_path)
    lifecycle.stale_memory()
    manifest_path = lifecycle.isolated.home / ".apm" / "apm.yml"
    manifest = load_yaml(manifest_path)
    assert manifest is not None
    manifest["target"] = "claude"
    manifest.pop("targets", None)
    dump_yaml_roundtrip(manifest, manifest_path)
    sources = list(
        (lifecycle.isolated.home / ".apm" / "apm_modules").rglob("style.instructions.md")
    )
    assert len(sources) == 1
    native = lifecycle.rules / "style.md"
    for path in (sources[0], native):
        path.write_text(
            path.read_text(encoding="utf-8").replace(_STYLE, f"{_STYLE}{hidden}"),
            encoding="utf-8",
        )
    roots = {"claude": lifecycle.claude_root, "codex": lifecycle.codex.parent}
    before = ArtifactSnapshotSet.capture(roots)

    result = lifecycle.run("compile", "-g", *flags, expected_returncode=expected_returncode)

    message = " ".join((result.stdout + result.stderr).split())
    if expected_returncode:
        assert "critical hidden characters" in message
    else:
        assert "hidden characters" in message
        assert "apm audit" in message
    assert ArtifactSnapshotSet.capture(roots) == before


@pytest.mark.parametrize(
    "ownership", ["hand-authored", "edited-generated", "valid-prior-generation"]
)
def test_clean_preserves_personal_memory_bytes(
    tmp_path: Path, apm_binary_path: Path, ownership: str
) -> None:
    """Neither a missing marker nor a retained marker authorizes deleting edits."""
    lifecycle = _create_lifecycle(tmp_path, apm_binary_path)
    content = b"# Personal memory\nKeep my manual instructions.\n"
    if ownership == "edited-generated":
        content = lifecycle.stale_memory() + b"\n# Personal edit\nNever delete this addition.\n"
    elif ownership == "valid-prior-generation":
        content = lifecycle.stale_memory()
        source = lifecycle.package / ".apm" / "instructions" / "style.instructions.md"
        source.write_text(
            source.read_text(encoding="utf-8").replace(_STYLE, "UPDATED-STYLE-SENTINEL"),
            encoding="utf-8",
        )
        lifecycle.install()
        assert "UPDATED-STYLE-SENTINEL" in (lifecycle.rules / "style.md").read_text(
            encoding="utf-8"
        )
    lifecycle.memory.write_bytes(content)
    native_before = {path.name: path.read_bytes() for path in lifecycle.rules.glob("*.md")}

    result = lifecycle.run("compile", "-g")
    assert lifecycle.memory.read_bytes() == content
    assert "retained redundant" not in result.stdout
    assert "--clean --dry-run" not in result.stdout
    expected = "hand-authored" if ownership == "hand-authored" else "edited or unverifiable"
    assert expected in result.stdout
    roots = {"claude": lifecycle.claude_root, "codex": lifecycle.codex.parent}
    before_preview = ArtifactSnapshotSet.capture(roots)
    lifecycle.run("compile", "-g", "--clean", "--dry-run")
    assert ArtifactSnapshotSet.capture(roots) == before_preview
    lifecycle.run("compile", "-g", "--clean")
    assert lifecycle.memory.read_bytes() == content
    assert {path.name: path.read_bytes() for path in lifecycle.rules.glob("*.md")} == native_before


@pytest.mark.parametrize("escaped", [False, True], ids=["within-config", "outside-config"])
def test_clean_preserves_symlinked_memory_and_its_destination(
    tmp_path: Path, apm_binary_path: Path, escaped: bool
) -> None:
    """Even an untouched generated destination cannot authorize unlinking a link."""
    lifecycle = _create_lifecycle(tmp_path, apm_binary_path)
    content = lifecycle.stale_memory()
    destination_root = lifecycle.isolated.work_root if escaped else lifecycle.claude_root
    destination = destination_root / "personal-memory.md"
    lifecycle.memory.rename(destination)
    try:
        lifecycle.memory.symlink_to(destination)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")

    result = lifecycle.run("compile", "-g", "--clean", expected_returncode=int(escaped))

    assert lifecycle.memory.is_symlink()
    assert lifecycle.memory.readlink() == destination
    assert destination.read_bytes() == content
    if escaped:
        assert "outside" in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.parametrize("link_kind", ["rule-inside", "rule-outside", "rules-directory"])
def test_symlinked_native_rules_retain_fallback_and_preserve_destination(
    tmp_path: Path, apm_binary_path: Path, link_kind: str
) -> None:
    """Equivalent bytes reached through a symlink cannot authorize suppression."""
    lifecycle = _create_lifecycle(tmp_path, apm_binary_path)
    native_before = {path.name: path.read_bytes() for path in lifecycle.rules.glob("*.md")}
    style = lifecycle.rules / "style.md"
    if link_kind == "rules-directory":
        link = lifecycle.rules
        destination = lifecycle.isolated.work_root / "saved-rules"
        link.rename(destination)
    else:
        link = style
        destination_root = (
            lifecycle.rules if link_kind == "rule-inside" else lifecycle.isolated.work_root
        )
        destination = destination_root / "saved-style.md"
        link.rename(destination)
    try:
        link.symlink_to(destination, target_is_directory=link_kind == "rules-directory")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")
    linked_before = {path.name: path.read_bytes() for path in lifecycle.rules.glob("*.md")}

    lifecycle.run("compile", "-g", "--clean")

    fallback = lifecycle.memory.read_text(encoding="utf-8")
    assert _STYLE in fallback
    if link_kind == "rules-directory":
        assert _WORKFLOW in fallback
        assert {path.name: path.read_bytes() for path in destination.glob("*.md")} == native_before
    else:
        assert _WORKFLOW not in fallback
        assert destination.read_bytes() == native_before["style.md"]
    assert link.is_symlink()
    assert link.readlink() == destination
    assert {path.name: path.read_bytes() for path in lifecycle.rules.glob("*.md")} == linked_before


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX directory permissions require an unprivileged process",
)
def test_cleanup_permission_failure_preserves_output_and_reports_the_problem(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """An unlink failure must be a safe diagnostic, not lost content or a traceback."""
    lifecycle = _create_lifecycle(tmp_path, apm_binary_path)
    content = lifecycle.stale_memory()
    original_mode = lifecycle.claude_root.stat().st_mode
    lifecycle.claude_root.chmod(0o555)
    try:
        result = lifecycle.runner.run(
            ("compile", "-g", "--clean"),
            scenario_id="global-claude-clean-permission-denied",
            cwd=lifecycle.isolated.work_root,
            env=lifecycle.environment,
        )
        assert result.returncode == 1, f"{result.stdout}\n{result.stderr}"
        assert lifecycle.memory.read_bytes() == content
        message = result.stdout + result.stderr
        assert "CLAUDE.md" in message
        assert "permission" in message.lower() or "denied" in message.lower()
        assert "Traceback" not in message
    finally:
        lifecycle.claude_root.chmod(original_mode)


def test_project_and_global_native_rules_do_not_cross_scope(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Project compilation remains unchanged and neither scope borrows rule evidence."""
    lifecycle = _create_lifecycle(tmp_path, apm_binary_path)
    project = LocalPackageFactory(lifecycle.isolated.work_root).create(
        "consumer", targets=("claude", "codex")
    )
    lifecycle.run(
        "install",
        str(lifecycle.package),
        "--target",
        "claude,codex",
        "--no-policy",
        "--parallel-downloads",
        "0",
        cwd=project.root,
    )
    project_rules = project.root / ".claude" / "rules"
    project_style = project_rules / "style.md"
    assert _STYLE in project_style.read_text(encoding="utf-8")
    for path in project_rules.glob("*.md"):
        path.unlink()

    lifecycle.run("compile", "--target", "claude,codex", cwd=project.root)

    project_memory = project.root / "CLAUDE.md"
    project_before = project_memory.read_bytes()
    assert _STYLE.encode() in project_before
    assert _WORKFLOW.encode() in project_before
    for path in lifecycle.rules.glob("*.md"):
        (project_rules / path.name).write_bytes(path.read_bytes())
    for path in lifecycle.rules.glob("*.md"):
        path.unlink()

    lifecycle.run("compile", "-g", "--clean", cwd=project.root)

    global_memory = lifecycle.memory.read_bytes()
    assert _STYLE.encode() in global_memory
    assert _WORKFLOW.encode() in global_memory
    assert project_memory.read_bytes() == project_before
    lifecycle.run(
        "compile",
        "--target",
        "claude,codex",
        "--clean",
        "--force-instructions",
        cwd=project.root,
    )
    assert project_memory.read_bytes() == project_before
    assert lifecycle.memory.read_bytes() == global_memory
