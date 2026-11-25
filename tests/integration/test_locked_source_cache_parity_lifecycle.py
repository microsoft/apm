"""One locked-source verdict for every materialized copy of a git package (#4356, #4353, #4363).

PROMISE: I3 -- ``install --frozen`` and ``audit --ci`` reach the same verdict
on the same state; I7 -- a refused command preserves durable state and says
why (source: .agents/skills/apm-lifecycle-tests/SKILL.md; owner
``package-content-hash`` in .apm/architecture/owners/install-deployment.json).
CELL: OP=audit-ci|install-frozen|install|update SRC=git PRIM=apm-package
SCOPE=project LOCK=unchanged DEPLOYED=warm CACHE=every-copy-differs|commit-unavailable FROZEN

When the project copy under ``apm_modules`` and the persistent git checkout
both differ from the locked commit, every command still reads the locked
source through the same cache. ``audit --ci`` is read-only: it names the
mismatch, reports the cache copy that does not match its commit and fails
without repairing anything (#5159). A writing command rebuilds the cache copy
from the commit and says so with a warning; the ``apm_modules`` copy is then
detected and restored as when it alone differs, and a later ``audit --ci``
passes. When the commit can no longer be fetched, the
cache copy cannot be rebuilt: every command refuses, names the package, and
leaves the project, the lockfile and ``apm_modules`` byte for byte as they were.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration._install_audit_replay_kit import (
    ReplayWorld,
    Shape,
    apm_package,
    tree_digest,
)
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I3", "P-OWN-package-content-hash"),
]

_TARGETS = ("copilot",)
_SKILL = Path(".apm") / "skills" / "helper" / "SKILL.md"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _all_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _durable(world: ReplayWorld) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Project files (with the lockfile) and every apm_modules file, byte for byte."""
    return tree_digest(world.project), _all_files(world.project / "apm_modules")


def _checkouts(world: ReplayWorld) -> Path:
    return world.isolated.cache_root / "git" / "checkouts_v1"


def _copies(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob(_SKILL.name)
        if ".git" not in path.parts and path.is_file() and path.parent.name == "helper"
    ]


def _edit_every_copy(world: ReplayWorld) -> bytes:
    """Edit the apm_modules copy and the cache copy; return the committed bytes."""
    modules, cached = _copies(world.project / "apm_modules"), _copies(_checkouts(world))
    assert modules and cached, (modules, cached)
    committed = modules[0].read_bytes()
    for path in (*modules, *cached):
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b"\nCopy edit.\n")
    return committed


def _assert_holds_commit(world: ReplayWorld, committed: bytes) -> None:
    """The cache copy was rebuilt and the apm_modules copy restored to the commit's bytes."""
    modules, cached = _copies(world.project / "apm_modules"), _copies(_checkouts(world))
    assert modules and cached, (modules, cached)
    for path in (*modules, *cached):
        assert path.read_bytes() == committed, path


def _drop_commit_content(world: ReplayWorld) -> None:
    """Remove one blob of the locked commit from the served repository."""
    origin = world.repos["pkg"].origin
    blob = world.git(origin, "rev-parse", f"HEAD:{_SKILL.as_posix()}").strip()
    loose = origin / "objects" / blob[:2] / blob[2:]
    assert loose.is_file(), loose
    loose.chmod(0o644)
    loose.unlink()


def _text(result: CommandResult) -> str:
    return re.sub(r"\s+", " ", result.stdout + result.stderr)


def _names_mismatch(result: CommandResult, expected: str) -> bool:
    return f"Content hash mismatch for test/pkg: expected {expected}, got sha256:" in _text(result)


def _mismatch_count(result: CommandResult) -> int:
    return _text(result).count("Content hash mismatch for test/pkg:")


def _rebuilt(result: CommandResult) -> bool:
    """The warning that names the cache copy rebuilt from its commit."""
    return (
        re.search(
            r"Cached copy of test/pkg \(\S+ @ [0-9a-f]{12}\) failed verification against its commit",
            _text(result),
        )
        is not None
    )


def _names_failed_fetch(result: CommandResult) -> bool:
    """An [x] line itself names test/pkg and the failed fetch as the cause."""
    cause = re.compile(r"fetch|does not match its commit|could not", re.IGNORECASE)
    return any(
        line.lstrip().startswith("[x]") and "test/pkg" in line and cause.search(line)
        for line in (result.stdout + "\n" + result.stderr).splitlines()
    )


def _installed(tmp_path: Path, apm_binary_path: Path) -> ReplayWorld:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    world.manifest(Shape([world.publish("pkg", apm_package)], "test/pkg", targets=_TARGETS))
    world.apm("install")
    # A clean cache passes both readers.
    world.apm("audit", "--ci")
    world.apm("install", "--frozen")
    return world


def _locked_content_hash(world: ReplayWorld) -> str:
    rows = load_yaml(world.project / "apm.lock.yaml")["dependencies"]
    return next(row["content_hash"] for row in rows if row["repo_url"] == "test/pkg")


def test_audit_ci_and_frozen_agree_when_every_copy_differs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _installed(tmp_path, apm_binary_path)
    expected = _locked_content_hash(world)
    committed = _edit_every_copy(world)
    before = _durable(world)
    project_before = before[0]

    # Paired probe on one state. audit --ci never writes, not even the cache:
    # it reports the tampered cache and fails; the writer rebuilds it (#5159).
    audit = world.apm("audit", "--ci", expect=None)
    assert _durable(world) == before, "I5: audit --ci changed durable state"
    assert audit.returncode != 0, _evidence(audit)
    assert not _rebuilt(audit), _evidence(audit)
    frozen = world.apm("install", "--frozen", expect=None)
    assert frozen.returncode == 0, _evidence(frozen)
    for result in (audit, frozen):
        # The apm_modules copy is named as when it alone differs.
        assert _names_mismatch(result, expected), _evidence(result)

    # The rebuild is announced, and both copies hold the commit's bytes again.
    assert _rebuilt(frozen), _evidence(frozen)
    _assert_holds_commit(world, committed)
    assert tree_digest(world.project) == project_before, "project or lockfile changed"

    world.apm("audit", "--ci")


def test_audit_ci_and_frozen_refuse_alike_when_the_commit_cannot_be_fetched(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _installed(tmp_path, apm_binary_path)
    expected = _locked_content_hash(world)
    _edit_every_copy(world)
    _drop_commit_content(world)
    before = _durable(world)

    # I3 paired probe on one state.
    audit = world.apm("audit", "--ci", expect=None)
    assert _durable(world) == before, "I5: audit --ci changed durable state"
    frozen = world.apm("install", "--frozen", expect=None)
    assert _durable(world) == before, "I7: refused frozen install changed durable state"
    plain = world.apm("install", expect=None)
    assert _durable(world) == before, "I7: refused install changed durable state"

    assert audit.returncode != 0, _evidence(audit)
    assert _names_mismatch(audit, expected), _evidence(audit)
    # The first writer tries the rebuild, says so, then refuses.
    assert _rebuilt(frozen), _evidence(frozen)
    for refused in (frozen, plain):
        assert refused.returncode != 0, _evidence(refused)
    # The [x] line names the failed fetch: see the cell below.


def test_frozen_refusal_names_the_failed_fetch_as_its_cause(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _installed(tmp_path, apm_binary_path)
    _edit_every_copy(world)
    _drop_commit_content(world)

    frozen = world.apm("install", "--frozen", expect=None)

    assert frozen.returncode != 0, _evidence(frozen)
    assert _names_failed_fetch(frozen), "no [x] line names the failed fetch\n" + _evidence(frozen)


def test_update_rebuilds_a_modified_cache_copy_with_a_warning(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _installed(tmp_path, apm_binary_path)
    committed = _edit_every_copy(world)
    project_before = tree_digest(world.project)

    update = world.apm("update", "--yes", expect=None)

    assert update.returncode == 0, _evidence(update)
    assert _rebuilt(update), _evidence(update)
    _assert_holds_commit(world, committed)
    assert tree_digest(world.project) == project_before, "project or lockfile changed"


def _assert_restore_only(result: CommandResult) -> None:
    """RA-004 rule B: the refusal offers only restores, never a lock edit."""
    text = re.sub(r"\s+", " ", result.stdout + result.stderr)
    assert "run 'apm cache clean --yes' to discard the cached package copies" in text, text
    assert "from version control (for example 'git checkout -- " in text, text
    for forbidden in ("entry from", "re-pin", "aside"):
        assert forbidden not in text, (forbidden, text)


def test_every_copy_differs_converges_without_a_remedy(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """RA-005: one mismatch report; the rebuilt cache converges with no printed remedy."""
    world = _installed(tmp_path, apm_binary_path)
    _edit_every_copy(world)

    audit = world.apm("audit", "--ci", expect=None)
    # Read-only: the tampered cache is reported, never rebuilt (#5159).
    assert audit.returncode != 0, _evidence(audit)
    # RA-005: one finding prints once.
    assert _mismatch_count(audit) == 1, _evidence(audit)
    rebuilt = world.apm("install")
    assert _rebuilt(rebuilt), _evidence(rebuilt)

    # Converged: nothing left to rebuild, restore or report.
    again = world.apm("install")
    assert not _rebuilt(again), _evidence(again)
    assert _mismatch_count(again) == 0, _evidence(again)
    clean = world.apm("audit", "--ci")
    assert clean.returncode == 0, _evidence(clean)
    assert _mismatch_count(clean) == 0, _evidence(clean)


def test_content_hash_refusal_lock_restore_converges(tmp_path: Path, apm_binary_path: Path) -> None:
    """RA-004: a changed lock is restored from version control, never edited."""
    world = _installed(tmp_path, apm_binary_path)
    world.git(world.project, "init", "-q", "--initial-branch=main")
    world.git(world.project, "add", "apm.yml", "apm.lock.yaml")
    world.git(world.project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "lock")
    lock = world.project / "apm.lock.yaml"
    expected = _locked_content_hash(world)
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(expected, "sha256:" + "0" * 64),
        encoding="utf-8",
    )

    refused = world.apm("install", expect=1)
    _assert_restore_only(refused)

    world.git(world.project, "checkout", "--", "apm.lock.yaml")
    world.apm("install")
    world.apm("audit", "--ci")


def test_kept_file_remedies_converge_without_force(tmp_path: Path, apm_binary_path: Path) -> None:
    """RA-008: a Kept file offers export-patch or move-aside, never --force; both converge."""
    world = _installed(tmp_path, apm_binary_path)
    deployed = world.project / ".github" / "instructions" / "glob.instructions.md"
    original = deployed.read_bytes()
    deployed.write_bytes(original + b"\nLocal edit.\n")

    kept = world.apm("install", expect=None)
    text = re.sub(r"\s+", " ", kept.stdout + kept.stderr)
    assert "Kept .github/instructions/glob.instructions.md: " in text, _evidence(kept)
    assert (
        "Move it aside, then run 'apm install' to restore it, or 'apm export-patch' to "
        "export the local edits as upstream patches."
    ) in text, _evidence(kept)
    assert "--force" not in text, _evidence(kept)

    # Keep the edit, as printed: the patch carries it upstream.
    world.apm("export-patch")
    patches = "".join(
        path.read_text(encoding="utf-8") for path in (world.project / "patches").rglob("*.patch")
    )
    assert "+Local edit." in patches, patches

    # Restore, as printed: move it aside, then install.
    deployed.rename(deployed.with_name(f"{deployed.name}.aside"))
    restored = world.apm("install")
    assert "Kept " not in restored.stdout + restored.stderr, _evidence(restored)
    assert deployed.read_bytes() == original
    world.apm("audit", "--ci")
