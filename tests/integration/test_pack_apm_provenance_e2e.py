"""Hermetic e2e coverage for provenance hardening of ``apm pack --format apm``.

The archive (``apm``) format is the default and most-used pack format
(consumed by apm-action + CI). This suite proves that dependency files are
verified against the per-file SHA-256 recorded in ``apm.lock.yaml``
(``deployed_file_hashes``) before they enter the bundle -- a deployed file
tampered after ``apm install`` must fail the pack loudly rather than ship
silently. It also proves cross-target path mapping does not produce false
mismatches, and that a directory symlink planted inside a deployed dir cannot
escape the project root into the bundle.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.utils.content_hash import compute_file_hash

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_e2e_mode,
]


def _write_apm_yml(project: Path, target: str = "copilot") -> None:
    project.joinpath("apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "apm-provenance-e2e",
                "version": "1.0.0",
                "description": "Hermetic apm-format provenance fixture",
                "target": target,
                "dependencies": {
                    "apm": [{"git": "acme/skill-bundle", "ref": "abc123"}],
                    "mcp": [],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    # Seed a copilot detection signal so target resolution succeeds.
    github = project / ".github"
    github.mkdir(exist_ok=True)
    (github / "copilot-instructions.md").write_text("# provenance fixture\n", encoding="utf-8")


def _write_deployed_skill(project: Path, name: str, marker: str) -> list[str]:
    """Write a deployed skill under .github/skills/<name>/ and return rel paths."""
    skill_dir = project / ".github" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(marker, encoding="utf-8")
    (skill_dir / "notes.md").write_text(f"{marker} notes", encoding="utf-8")
    return [
        (skill_dir / "SKILL.md").relative_to(project).as_posix(),
        (skill_dir / "notes.md").relative_to(project).as_posix(),
    ]


def _dep_with_hashes(project: Path, deployed_files: list[str]) -> LockedDependency:
    """Build a locked dep whose deployed_file_hashes match current on-disk bytes."""
    hashes = {
        rel: compute_file_hash(project / rel) for rel in deployed_files if (project / rel).is_file()
    }
    return LockedDependency(
        repo_url="acme/skill-bundle",
        resolved_commit="abc123",
        depth=1,
        package_type="skill_bundle",
        deployed_files=deployed_files,
        deployed_file_hashes=hashes,
    )


def _write_lockfile(project: Path, dep: LockedDependency) -> None:
    lockfile = LockFile()
    lockfile.add_dependency(dep)
    lockfile.write(project / "apm.lock.yaml")


def _run_pack_apm(
    project: Path, target: str | None = None, output_name: str = "build"
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("GITHUB_TOKEN", "GITHUB_APM_PAT", "ADO_APM_PAT"):
        env.pop(name, None)
    home = project / ".home"
    home.mkdir(exist_ok=True)
    env.update({"APM_E2E_TESTS": "1", "HOME": str(home), "PYTHONUTF8": "1"})
    args = [
        sys.executable,
        "-m",
        "apm_cli.cli",
        "pack",
        "--format",
        "apm",
        "--output",
        output_name,
    ]
    if target is not None:
        args += ["--target", target]
    return subprocess.run(
        args,
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _bundle_dir(project: Path, output_name: str = "build") -> Path:
    return project / output_name / "apm-provenance-e2e-1.0.0"


def test_apm_pack_accepts_matching_deployed_file(tmp_path: Path) -> None:
    """Untampered deployed files pack cleanly (guards against false positives)."""
    project = tmp_path / "project"
    project.mkdir()
    _write_apm_yml(project)
    deployed = _write_deployed_skill(project, "alpha", "deployed alpha")
    _write_lockfile(project, _dep_with_hashes(project, deployed))

    result = _run_pack_apm(project)

    assert result.returncode == 0, result.stdout + result.stderr
    bundle = _bundle_dir(project)
    assert (bundle / ".github" / "skills" / "alpha" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "deployed alpha"


def test_apm_pack_rejects_tampered_deployed_file(tmp_path: Path) -> None:
    """A deployed file tampered after install must fail the pack loudly.

    This is the RED-without-fix / GREEN-with-fix provenance gate for the
    default archive format: the recorded SHA-256 no longer matches the on-disk
    bytes, so the file must never enter the bundle silently.
    """
    project = tmp_path / "project"
    project.mkdir()
    _write_apm_yml(project)
    deployed = _write_deployed_skill(project, "alpha", "deployed alpha")
    _write_lockfile(project, _dep_with_hashes(project, deployed))

    # Tamper the deployed file AFTER the lockfile recorded its attested hash.
    (project / ".github" / "skills" / "alpha" / "SKILL.md").write_text(
        "tampered payload", encoding="utf-8"
    )

    result = _run_pack_apm(project)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "does not match the hash recorded in apm.lock.yaml" in combined
    packed = _bundle_dir(project) / ".github" / "skills" / "alpha" / "SKILL.md"
    assert not packed.exists() or packed.read_text(encoding="utf-8") != "tampered payload"


def test_apm_pack_rejects_tampered_file_in_deployed_directory(tmp_path: Path) -> None:
    """A tampered file inside a deployed *directory* entry also fails loud.

    Directory entries are copied via copytree; the provenance walk verifies
    each contained file against its attested hash before the copy.
    """
    project = tmp_path / "project"
    project.mkdir()
    _write_apm_yml(project)
    _write_deployed_skill(project, "alpha", "deployed alpha")
    skill_dir = project / ".github" / "skills" / "alpha"
    # Hash the contained files, but list the DIRECTORY in deployed_files.
    file_rels = [
        (skill_dir / "SKILL.md").relative_to(project).as_posix(),
        (skill_dir / "notes.md").relative_to(project).as_posix(),
    ]
    hashes = {rel: compute_file_hash(project / rel) for rel in file_rels}
    dep = LockedDependency(
        repo_url="acme/skill-bundle",
        resolved_commit="abc123",
        depth=1,
        package_type="skill_bundle",
        deployed_files=[skill_dir.relative_to(project).as_posix() + "/"],
        deployed_file_hashes=hashes,
    )
    _write_lockfile(project, dep)

    (skill_dir / "notes.md").write_text("tampered notes", encoding="utf-8")

    result = _run_pack_apm(project)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "does not match the hash recorded in apm.lock.yaml" in combined


def test_apm_pack_cross_target_mapped_file_no_false_positive(tmp_path: Path) -> None:
    """Cross-target mapped files verify against the on-disk key, not bundle key.

    Files deployed under ``.github/skills/`` are hashed against their on-disk
    (``.github``) path. Packing for the ``claude`` target remaps the bundle
    path to ``.claude/skills/`` while still reading from disk under
    ``.github``. Verification must look up the on-disk key, so an untampered
    mapped file must NOT be reported as a mismatch.
    """
    project = tmp_path / "project"
    project.mkdir()
    _write_apm_yml(project, target="claude")
    deployed = _write_deployed_skill(project, "alpha", "deployed alpha")
    _write_lockfile(project, _dep_with_hashes(project, deployed))

    result = _run_pack_apm(project, target="claude")

    assert result.returncode == 0, result.stdout + result.stderr
    bundle = _bundle_dir(project)
    # Remapped into the claude layout, content intact.
    assert (bundle / ".claude" / "skills" / "alpha" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "deployed alpha"


def test_apm_pack_directory_symlink_does_not_escape(tmp_path: Path) -> None:
    """A directory symlink inside a deployed dir must not leak external bytes.

    Defense-in-depth: the copytree ignore filter drops symlinks and the
    provenance walk re-asserts containment, so a planted directory symlink
    whose target sits outside the project root contributes nothing to the
    bundle.
    """
    project = tmp_path / "project"
    project.mkdir()
    _write_apm_yml(project)
    deployed = _write_deployed_skill(project, "alpha", "deployed alpha")

    # Secret material outside the project root.
    secret_dir = tmp_path / "outside"
    secret_dir.mkdir()
    (secret_dir / "secret.md").write_text("TOP SECRET", encoding="utf-8")

    # Plant a directory symlink INSIDE the deployed skill dir pointing outside.
    skill_dir = project / ".github" / "skills" / "alpha"
    link = skill_dir / "leak"
    try:
        link.symlink_to(secret_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support directory symlinks")

    dep = LockedDependency(
        repo_url="acme/skill-bundle",
        resolved_commit="abc123",
        depth=1,
        package_type="skill_bundle",
        deployed_files=[skill_dir.relative_to(project).as_posix() + "/"],
        deployed_file_hashes={rel: compute_file_hash(project / rel) for rel in deployed},
    )
    _write_lockfile(project, dep)

    result = _run_pack_apm(project)

    # Two acceptable outcomes prove the same invariant across Python versions:
    #  (a) pack succeeds and the symlinked secret simply never lands (rglob
    #      does not descend directory symlinks on 3.12+), or
    #  (b) the per-child containment guard fires and pack fails loud with an
    #      escape error (stronger refusal).
    # Either way, the external secret must NOT enter the bundle.
    combined = result.stdout + result.stderr
    if result.returncode != 0:
        assert "escapes project root" in combined, combined
    else:
        bundle = _bundle_dir(project)
        assert not (bundle / ".github" / "skills" / "alpha" / "leak").exists()
        leaked = list(bundle.rglob("secret.md"))
        assert not leaked, f"external secret leaked into bundle: {leaked}"
