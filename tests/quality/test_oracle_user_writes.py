"""I1-X2 and I4-X2: a declared user write is the user's, and APM must keep it (#4101).

A scenario that edits a deployed file, or writes its own file at a package's
path, declares those bytes (``Scenario.user_write``). I1 exempts the path from
lock==disk only while disk holds exactly the declared bytes, and an exempted
step earns no I1 receipt. I4-X2 exempts a gate surface holding only such
bytes only when the step announces the keep and ``audit --ci`` fails naming
it. These negative controls keep both exceptions narrow.
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
