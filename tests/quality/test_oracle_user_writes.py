"""I1-X2, I4-X2 and I7-X2: a declared user write is the user's, and APM must keep it (#4101).

A scenario that edits a deployed file, or writes its own file at a package's
path, declares those bytes (``Scenario.user_write``). I1 exempts the path from
lock==disk only while disk holds exactly the declared bytes, and an exempted
step earns no I1 receipt. I4-X2 exempts a gate surface holding only such
bytes only when the step announces the keep and ``audit --ci`` fails naming
it. I7-X2 lets a failed install that kept an edit (#4626) change only the
lockfile and the files it lists as written. These negative controls keep the
exceptions narrow.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.utils.content_hash import compute_file_hash
from tests.lifecycle import oracles
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.lifecycle_state import LifecycleStateSnapshot

RATCHET_TEST_SCOPE = "repository"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-INV-I1"),
    pytest.mark.promise("P-INV-I4"),
    pytest.mark.promise("P-INV-I7"),
]

_SKILL = ".agents/skills/p-skill/SKILL.md"
_APM_BYTES = b"---\nname: p-skill\n---\n\nUse p-skill.\n"
_USER_BYTES = b"---\nname: p-skill\n---\n\nUse p-skill.\nmy edit\n"


def _i1(root: Path, *, user: dict[str, bytes], lock: LockFile | None, managed=()) -> object:
    observation = oracles.Observation(
        LifecycleStateSnapshot.capture(root),
        ArtifactSnapshotSet.capture({"project": root}),
        frozenset(),
        root,
        lock,
        frozenset(managed),
        user_bytes={path: hashlib.sha256(data).hexdigest() for path, data in user.items()},
    )
    return oracles._CHECKS["I1"](observation, observation, None, None, None)


def _deployed(root: Path) -> LockFile:
    """A lock recording APM's bytes for the skill, then the user's edit on disk."""
    path = root / _SKILL
    path.parent.mkdir(parents=True)
    path.write_bytes(_APM_BYTES)
    digest = compute_file_hash(path)
    dependency = LockedDependency(
        repo_url="owner/p-skill", deployed_files=[_SKILL], deployed_file_hashes={_SKILL: digest}
    )
    path.write_bytes(_USER_BYTES)
    return LockFile(dependencies={"owner/p-skill": dependency})


def test_an_undeclared_edit_of_a_deployed_file_still_fails_i1(tmp_path: Path) -> None:
    lock = _deployed(tmp_path)

    violations = _i1(tmp_path, user={}, lock=lock)

    assert [v.message for v in violations] == [f"recorded hash does not match disk: {_SKILL}"]


def test_a_declared_edit_the_cli_changed_fails_i1(tmp_path: Path) -> None:
    lock = _deployed(tmp_path)
    (tmp_path / _SKILL).write_bytes(_USER_BYTES + b"rewritten\n")

    violations = _i1(tmp_path, user={_SKILL: _USER_BYTES}, lock=lock)

    assert f"I1-X2: the user's bytes were changed: {_SKILL}" in [v.message for v in violations]


def test_a_declared_user_file_the_cli_removed_fails_i1(tmp_path: Path) -> None:
    violations = _i1(tmp_path, user={_SKILL: _USER_BYTES}, lock=None)

    assert [v.message for v in violations] == [f"I1-X2: the user's file was removed: {_SKILL}"]


def test_a_kept_declared_edit_is_exempt_and_earns_no_receipt(tmp_path: Path) -> None:
    lock = _deployed(tmp_path)

    assert _i1(tmp_path, user={_SKILL: _USER_BYTES}, lock=lock) is None


def test_a_kept_user_file_at_a_package_path_is_exempt(tmp_path: Path) -> None:
    path = tmp_path / _SKILL
    path.parent.mkdir(parents=True)
    path.write_bytes(_USER_BYTES)

    assert _i1(tmp_path, user={_SKILL: _USER_BYTES}, lock=LockFile(), managed=[_SKILL]) is None
    undeclared = _i1(tmp_path, user={}, lock=LockFile(), managed=[_SKILL])
    assert [v.message for v in undeclared] == [f"deployed file is not in the lock: {_SKILL}"]


_HOOK = ".github/hooks/p-exec-session.json"
_CELL = "p-exec/hook/copilot"
_KEPT = f"[!] Kept user-edited file {_HOOK} (from _local/p-exec)"
_DETECTED = (1, f"[!] Drift detected: 1 file(s) orphaned (1):\n  - {_HOOK}")


class _NothingAllowed:
    def allowed_surfaces(self) -> frozenset[str]:
        return frozenset()


def _i4(root: Path, *, user: bool, output: str, audit: tuple[int, str] | None) -> object:
    """I4 after an install that left the user-edited hook of a denied package."""
    path = root / _HOOK
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"hooks": {"echo": "my-own-hook"}}\n')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    observation = oracles.Observation(
        LifecycleStateSnapshot.capture(root),
        ArtifactSnapshotSet.capture({"project": root}),
        frozenset({_CELL}),
        root,
        None,
        user_bytes={_HOOK: digest} if user else {},
        cell_paths={_CELL: frozenset({_HOOK})},
        audit_ci=audit,
    )
    result = SimpleNamespace(returncode=0, stdout=output, stderr="")
    op = oracles.Operation("install", mutating=True)
    return oracles._CHECKS["I4"](observation, observation, result, _NothingAllowed(), op)


def test_undeclared_bytes_on_a_closed_gate_fail_i4(tmp_path: Path) -> None:
    violations = _i4(tmp_path, user=False, output=_KEPT, audit=_DETECTED)

    assert [v.message for v in violations] == [f"{_CELL} still deployed but not allowed"]


def test_an_unannounced_keep_fails_i4(tmp_path: Path) -> None:
    violations = _i4(tmp_path, user=True, output="[+] Installed", audit=_DETECTED)

    assert [v.message for v in violations] == [
        f"{_CELL} still deployed but not allowed (I4-X2: the keep is not announced: ['{_HOOK}'])"
    ]


def test_an_undetected_keep_fails_i4(tmp_path: Path) -> None:
    violations = _i4(tmp_path, user=True, output=_KEPT, audit=(0, "All 15 check(s) passed"))

    assert [v.message for v in violations] == [
        f"{_CELL} still deployed but not allowed "
        f"(I4-X2: an undetected keep: audit --ci exited 0, not naming ['{_HOOK}'])"
    ]


def test_an_announced_detected_keep_is_exempt_and_earns_no_receipt(tmp_path: Path) -> None:
    assert _i4(tmp_path, user=True, output=_KEPT, audit=_DETECTED) is None


_SCRIPT = ".github/hooks/scripts/o-exec/session/run.sh"
_KEPT_BLOCKED = (
    "  [x] 1 package failed:\n"
    "  +- o-exec -- Kept .github/extensions/o-exec/extension.mjs: it was modified since "
    "APM deployed it.\n"
)
_WROTE = f"This install wrote 1 file(s), recorded in apm.lock.yaml:\n  {_HOOK}\n"


def _wrote(*paths: str) -> str:
    listed = "".join(f"  {path}\n" for path in paths)
    return f"This install wrote {len(paths)} file(s), recorded in apm.lock.yaml:\n{listed}"


class _Allowed:
    def __init__(self, surfaces: frozenset[str]) -> None:
        self.surfaces = surfaces

    def allowed_surfaces(self) -> frozenset[str]:
        return self.surfaces


def _i7(
    root: Path,
    output: str,
    *,
    writes: tuple[str, ...],
    returncode: int = 1,
    surface: tuple[str, str] | None = None,
    symlink: str | None = None,
) -> list[str]:
    """I7 after a failed install that rewrote the lock and *writes*.

    *surface* is a ``(cell, path)`` the install newly deployed; *symlink* is a
    path the install newly made a symlink to a directory outside the root.
    """
    (root / "apm.lock.yaml").write_text("lockfile_version: '1'\n", encoding="ascii")
    for relative in (_HOOK, _SCRIPT):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text("v1\n", encoding="ascii")

    def observe(deployed: frozenset[str] = frozenset(), cells=None) -> object:
        return oracles.Observation(
            LifecycleStateSnapshot.capture(root),
            ArtifactSnapshotSet.capture({"project": root}),
            deployed,
            root,
            None,
            cell_paths=cells or {},
        )

    before = observe()
    (root / "apm.lock.yaml").write_text("lockfile_version: '1'\n# v2\n", encoding="ascii")
    for relative in writes:
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text("v2\n", encoding="ascii")
    if symlink is not None:
        outside = root.parent / "outside"
        outside.mkdir(exist_ok=True)
        (root / symlink).parent.mkdir(parents=True, exist_ok=True)
        (root / symlink).symlink_to(outside, target_is_directory=True)
    cells = {surface[0]: frozenset({surface[1]})} if surface else {}
    after = observe(frozenset(cells), cells)
    result = SimpleNamespace(returncode=returncode, stdout=output, stderr="", command=("install",))
    op = oracles.operation_for(("install",))
    violations = oracles._CHECKS["I7"](before, after, result, _Allowed(frozenset(cells)), op)
    return [v.message for v in violations]


def test_a_kept_edit_install_may_write_the_lock_and_its_listed_files(tmp_path: Path) -> None:
    assert _i7(tmp_path, _KEPT_BLOCKED + _WROTE, writes=(_HOOK,)) == []


def test_a_kept_edit_install_never_admits_an_unlisted_write(tmp_path: Path) -> None:
    violations = _i7(tmp_path, _KEPT_BLOCKED + _WROTE, writes=(_HOOK, _SCRIPT))

    assert violations == [f"failed install changed durable files: ['project:{_SCRIPT}']"]


def test_a_failed_install_without_a_kept_line_preserves_the_lock(tmp_path: Path) -> None:
    violations = _i7(tmp_path, "[x] Install failed\n" + _WROTE, writes=(_HOOK,))

    assert violations == [
        "failed install changed the lockfile",
        f"failed install changed durable files: ['project:{_HOOK}']",
    ]


def test_the_kept_edit_exemption_needs_exit_code_one(tmp_path: Path) -> None:
    violations = _i7(tmp_path, _KEPT_BLOCKED + _WROTE, writes=(_HOOK,), returncode=2)

    assert "failed install changed the lockfile" in violations


_NEW_SCRIPT = ".github/hooks/scripts/o-exec/new/run.sh"
_CANVAS = ".github/extensions/o-canvas/extension.mjs"
_CANVAS_CELL = "o-canvas/canvas/copilot"


def test_a_kept_edit_install_may_create_the_directories_of_a_listed_write(tmp_path: Path) -> None:
    assert _i7(tmp_path, _KEPT_BLOCKED + _wrote(_NEW_SCRIPT), writes=(_NEW_SCRIPT,)) == []


def test_a_new_directory_never_covers_an_unlisted_write(tmp_path: Path) -> None:
    violations = _i7(tmp_path, _KEPT_BLOCKED + _wrote(_HOOK), writes=(_HOOK, _NEW_SCRIPT))

    new_dir = _NEW_SCRIPT.rsplit("/", 1)[0]
    assert violations == [
        f"failed install changed durable files: ['project:{new_dir}', 'project:{_NEW_SCRIPT}']"
    ]


def test_a_new_symlink_never_passes_as_the_directory_of_a_listed_write(tmp_path: Path) -> None:
    """I7-X2 exempts only a created real directory, not a symlink (#4626 review)."""
    root = tmp_path / "project"
    root.mkdir()
    new_dir = _NEW_SCRIPT.rsplit("/", 1)[0]
    violations = _i7(root, _KEPT_BLOCKED + _wrote(_NEW_SCRIPT), writes=(), symlink=new_dir)

    assert violations == [f"failed install changed durable files: ['project:{new_dir}']"]


def test_a_new_file_never_passes_as_the_directory_of_a_listed_write(tmp_path: Path) -> None:
    """I7-X2: a regular file at the ancestor path is an unlisted change (#4626 review)."""
    new_dir = _NEW_SCRIPT.rsplit("/", 1)[0]
    violations = _i7(tmp_path, _KEPT_BLOCKED + _wrote(_NEW_SCRIPT), writes=(new_dir,))

    assert violations == [f"failed install changed durable files: ['project:{new_dir}']"]


def test_a_kept_edit_install_may_deploy_a_new_surface_it_lists(tmp_path: Path) -> None:
    output = _KEPT_BLOCKED + _wrote(_CANVAS)

    assert _i7(tmp_path, output, writes=(_CANVAS,), surface=(_CANVAS_CELL, _CANVAS)) == []


def test_a_new_surface_not_listed_as_written_still_fails(tmp_path: Path) -> None:
    output = _KEPT_BLOCKED + _wrote(_HOOK)
    violations = _i7(tmp_path, output, writes=(_HOOK, _CANVAS), surface=(_CANVAS_CELL, _CANVAS))

    assert "failed install changed deployed surfaces" in violations


def test_a_listed_write_never_covers_the_same_path_in_another_root(tmp_path: Path) -> None:
    """I7-X2 resolves listed writes under the deploy root only (#4626 review)."""
    project, home = tmp_path / "project", tmp_path / "home"
    for root in (project, home):
        (root / _HOOK).parent.mkdir(parents=True)
        (root / _HOOK).write_text("v1\n", encoding="ascii")
    (project / "apm.lock.yaml").write_text("lockfile_version: '1'\n", encoding="ascii")

    def observe() -> object:
        return oracles.Observation(
            LifecycleStateSnapshot.capture(project),
            ArtifactSnapshotSet.capture({"project": project, "home": home}),
            frozenset(),
            project,
            None,
        )

    before = observe()
    (project / "apm.lock.yaml").write_text("lockfile_version: '1'\n# v2\n", encoding="ascii")
    for root in (project, home):
        (root / _HOOK).write_text("v2\n", encoding="ascii")
    output = _KEPT_BLOCKED + _wrote(_HOOK)
    result = SimpleNamespace(returncode=1, stdout=output, stderr="", command=("install",))
    op = oracles.operation_for(("install",))
    violations = oracles._CHECKS["I7"](before, observe(), result, _Allowed(frozenset()), op)

    assert [v.message for v in violations] == [
        f"failed install changed durable files: ['home:{_HOOK}']"
    ]
