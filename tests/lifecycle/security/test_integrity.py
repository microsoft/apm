"""Integrity: a changed lock, cache or upstream is detected, refused or repaired (#4109).

PROMISE: every recorded integrity anchor (deployed-file hashes, package
``content_hash``, approval pins, the policy hash pin, ...) either detects a
change (``apm audit --ci`` exits 1 naming the check), refuses it
(``install --frozen`` / ``install`` exit 1 and preserve state, P-INV-I7) or
repairs it, as the docs promise. Sources:
docs/src/content/docs/reference/baseline-checks.md (content-integrity, drift),
docs/src/content/docs/reference/cli/install.md (frozen mode),
docs/src/content/docs/reference/cli/update.md, docs/src/content/docs/enterprise/security.md,
docs/src/content/docs/reference/cli/approve.md (pinned approvals).

FRAMEWORK. One parametrized test runs every row of ``SLICES``. A row id is the
exact generated obligation ``I|<artifact>|<op>|<tamper>``
(``tests/lifecycle/obligation_space.py``). Each ``ArtifactSlice`` owns:

- ``arrange``: replay steps that converge the world (I1-I9 checked per step);
- ``tampers``: tamper id -> ``Tamper`` (the change, applied out of band);
- ``cells``: (op, tamper) -> ``Expect`` (exit, output, state facts).

To add an artifact, add one ``ArtifactSlice`` to ``SLICES``; no shared code
changes. ``OPS`` maps every op id to its command line.

Every op step runs through ``TamperReplay`` (``tests/lifecycle/rows.py``), so
``check_cross_cutting`` checks I1-I9. A tamper that edits a deployed file or
its recorded hash breaks I1 by construction; ``Tamper.lock_paths`` excuses I1
messages for exactly those paths (not a strict cell: an op may repair them).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver, oracles
from tests.lifecycle.model.expected import expected_exit
from tests.lifecycle.obligation_space import security_obligations
from tests.lifecycle.oracles import ExpectedCell, check_cross_cutting, operation_for
from tests.lifecycle.rows import _PROJECT_HOME_ALLOWED, _RECONCILING, Row, RowReplay, _RowModel
from tests.lifecycle.world import World
from tests.lifecycle.world.builder import TAG
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.promise(
        "P-OWN-package-content-hash",
        "P-INV-I3",
        "P-INV-I4",
        "P-INV-I7",
        "P-INV-I8",
    ),
]

LOCK = "apm.lock.yaml"
MANIFEST = "apm.yml"
SKILL = ".agents/skills/p-skill/SKILL.md"
HOOK = ".github/hooks/p-exec-session.json"
HOOK_SOURCE = ".apm/hooks/session.json"
STRENGTH = "e2e"

# op id -> command line (package names resolve to the slice's PackageRef spec).
OPS: Mapping[str, Callable[[str], str]] = {
    "audit-ci": lambda pkg: "apm audit --ci",
    "bundle-install": lambda pkg: f"apm install {pkg}",
    "frozen": lambda pkg: "apm install --frozen",
    "install": lambda pkg: "apm install",
    "uninstall-prune": lambda pkg: f"apm uninstall {pkg}",
    "update": lambda pkg: "apm update --yes",
}
# Ops that only read: I5 asserts they write nothing.
_READ_ONLY_OPS = frozenset({"audit-ci"})


# -- the replay: RowReplay plus excused tamper paths ----------------------------


class TamperReplay(RowReplay):
    """``RowReplay`` whose steps may excuse I1 messages of the tampered paths.

    After a tamper the reference model is no longer converged, so I2 does not
    apply to the op step; the op may legitimately repair or refuse.
    """

    def __init__(self, world, row: Row) -> None:
        super().__init__(world, row)
        self.excused: tuple[str, ...] = ()
        self.tampered = False

    def _args(self, line: str) -> tuple[str, ...]:
        # approve / deny take the version-blind identity (approve.md "Store format").
        args = super()._args(line)
        if args[0] in ("approve", "deny"):
            return tuple(arg.split("#", 1)[0] for arg in args)
        return args

    def _run(
        self,
        line: str,
        *,
        read_only: bool = False,
        paired: Mapping[str, int] | None = None,
        pinned_exit: int | None = None,
    ) -> CommandResult:
        # Mirrors RowReplay._run (tests/lifecycle/rows.py); adds the excused cells.
        requested = self._requested(driver.command_args(line))
        args = self._args(line)
        fields: dict[str, object] = {
            "forbidden": {"home": ("*",)},
            "allowed": {"home": _PROJECT_HOME_ALLOWED},
            "read_only": read_only,
            "paired": paired or {},
        }
        if args[0] not in _RECONCILING:
            fields["mutating"] = False
        op = operation_for(args, scope=self.row.scope, **fields)
        allowed_exit = (
            frozenset({pinned_exit})
            if pinned_exit is not None
            else expected_exit(args[0], requested, frozen="FROZEN" in op.modifiers)
        )
        cells = {
            "I1": tuple(
                ExpectedCell(f"tamper:{path}", re.escape(path) + "$", per_step=False)
                for path in self.excused
            )
        }
        converged = self.model.converged and requested == self.model and not self.tampered
        model = _RowModel(requested, self.kinds, converged, cells, self.expected_seen)
        before = self.observe()
        result = driver.run(self.world, args, scope=self.row.scope)
        after = self.observe()
        assert result.returncode in allowed_exit, (
            f"R: {line!r} exited {result.returncode}, wanted {sorted(allowed_exit)}\n"
            f"{result.stdout}\n{result.stderr}"
        )
        check_cross_cutting(before, after, result, model, op)
        self._advance(op, requested, result)
        self.last = result
        return result


# -- tamper helpers ------------------------------------------------------------


def _lock(world: World) -> dict:
    return yaml.safe_load((world.project / LOCK).read_text(encoding="utf-8"))


def _write_lock(world: World, data: dict) -> None:
    (world.project / LOCK).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _dependency(data: dict, repo: str) -> dict:
    return next(dep for dep in data["dependencies"] if dep["repo_url"] == repo)


def _replace_in_lock(world: World, old: str, new: str) -> None:
    path = world.project / LOCK
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def _append(path: Path, text: str) -> None:
    path.chmod(0o644)
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _package_copies(world: World, name: str) -> list[Path]:
    """Every materialized copy of *name* in apm_modules and the git checkout cache."""
    cache = (world.cache_root or world.root / "cache") / "git" / "checkouts_v1"
    roots = [world.project / "apm_modules", cache]
    return [
        path
        for root in roots
        if root.is_dir()
        for path in root.rglob(name)
        if ".git" not in path.parts and path.is_file()
    ]


def _served_repositories(world: World) -> dict[str, Path]:
    """The world's served ``owner/repo`` -> bare repository map.

    TODO(world): replace with a public ``WorldServices.override_repository(slug, path)``
    context manager; this is the only place the test reaches into the services.
    """
    return world.services._repositories


@contextlib.contextmanager
def _moved_upstream(world: World, slug: str, change: Callable[[Path], None]):
    """Serve *slug* from a private copy whose tag moved to a commit made by *change*.

    *change* edits a work tree of the repository; the shared per-worker map is
    swapped for this test only and restored after.
    """
    served = _served_repositories(world)
    original = served[slug]
    copy = world.root / "upstream" / slug.replace("/", "__")
    shutil.copytree(original, copy)
    work = world.root / "upstream-work" / slug.replace("/", "__")
    git = ("git", "-c", "user.name=lifecycle", "-c", "user.email=lifecycle@example.invalid")
    subprocess.run(("git", "clone", "-q", str(copy), str(work)), env=world.env, check=True)
    change(work)
    branch = subprocess.run(
        ("git", "--git-dir", str(copy), "symbolic-ref", "HEAD"),
        env=world.env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    for args in (
        ("add", "-A"),
        ("commit", "-qm", "moved"),
        ("tag", "-f", TAG),
        ("push", "-qf", "origin", f"HEAD:{branch}", f"refs/tags/{TAG}"),
    ):
        subprocess.run(
            (*git, "-C", str(work), *args), env=world.env, check=True, capture_output=True
        )
    served[slug] = copy
    try:
        yield
    finally:
        served[slug] = original


def _edit_files(name: str, edit: Callable[[str], str]) -> Callable[[Path], None]:
    """A work-tree change that rewrites every file called *name* through *edit*."""

    def change(work: Path) -> None:
        paths = [path for path in work.rglob(name) if ".git" not in path.parts]
        assert paths, f"no {name} in {work}"
        for path in paths:
            path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")

    return change


def _pending(phrase: str) -> str:
    """Reason of a strict expected failure whose tracking issue is maintainer-routed."""
    return f"S-pending (neutral issue to follow): {phrase}"


@dataclass(frozen=True)
class Tamper:
    """One out-of-band change; ``apply`` may register cleanups on the stack."""

    apply: Callable[[World, contextlib.ExitStack], None]
    # Deployed paths whose I1 messages the tamper itself causes.
    lock_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class Expect:
    """What one op does after one tamper."""

    exit: int
    output: tuple[str, ...] = ()  # regexes the combined output must match
    forbids: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()  # project path prefixes byte-identical across the op
    absent: tuple[str, ...] = ()
    present: tuple[str, ...] = ()
    # Extra state fact on the world after the op (raises AssertionError).
    check: Callable[[World], None] | None = None
    xfail: str | None = None  # _pending(<phrase>) -> strict expected failure
    smoke: bool = False


@dataclass(frozen=True)
class ArtifactSlice:
    """The rows of one integrity artifact."""

    packages: tuple[str, ...]  # world package keys; the first is the subject
    arrange: tuple[object, ...]  # replay steps that converge the world
    tampers: Mapping[str, Tamper]
    cells: Mapping[tuple[str, str], Expect]
    setup: Callable[[World], None] | None = None  # input edits before arrange
    # Steps after ``arrange`` that need the replay (for example an approval key).
    after_arrange: Callable[[World, TamperReplay], None] | None = None
    ops: Mapping[str, Callable[[str], str]] = field(default_factory=lambda: OPS)
    # Slice package name -> world package key (git sources by default, so the
    # upstream tamper is a real moved tag on the world's git server).
    package_keys: Mapping[str, str] = field(
        default_factory=lambda: {"p-skill": "p-skill:git", "p-exec": "p-exec:git"}
    )


# -- slice: deployed_file_hashes (p-skill from git) ------------------------------
# content-integrity compares deployed bytes with deployed_file_hashes
# (policy/ci_checks.py _check_content_integrity); --frozen checks them before
# the pipeline (install.md "Frozen mode fails rather than rewriting").


def _edit_deployed_skill(world: World, stack: contextlib.ExitStack) -> None:
    del stack
    _append(world.project / SKILL, "\nLocal edit.\n")


def _edit_recorded_skill_hash(world: World, stack: contextlib.ExitStack) -> None:
    del stack
    recorded = _dependency(_lock(world), "lifecycle/p-skill")["deployed_file_hashes"][SKILL]
    _replace_in_lock(world, recorded, "sha256:" + "1" * 64)


def _drop_recorded_skill_hash(world: World, stack: contextlib.ExitStack) -> None:
    del stack
    data = _lock(world)
    del _dependency(data, "lifecycle/p-skill")["deployed_file_hashes"]
    for row in data.get("deployments", ()):
        if row["value"] == SKILL:
            row["content_hash"] = None
    _write_lock(world, data)


def _move_skill_upstream(world: World, stack: contextlib.ExitStack) -> None:
    change = _edit_files("SKILL.md", lambda text: text + "\nMoved.\n")
    stack.enter_context(_moved_upstream(world, "lifecycle/p-skill", change))


def _skill_hash_matches_disk(world: World) -> None:
    recorded = _dependency(_lock(world), "lifecycle/p-skill").get("deployed_file_hashes", {})
    assert recorded.get(SKILL) == _sha256(world.project / SKILL), recorded


def _skill_moved_in(world: World) -> None:
    assert "Moved." in (world.project / SKILL).read_text(encoding="utf-8")
    _skill_hash_matches_disk(world)


_SKILL_STATE = (LOCK, MANIFEST, ".agents/skills/p-skill")
_DRIFT = r"content-integrity\s*\|\s*1 file\(s\) with hash drift"
_FROZEN_HASHES = r"--frozen: deployed files do not match the hashes recorded in apm.lock.yaml"
_RETAINED = r"Uninstall could not remove tracked target files"

DEPLOYED_FILE_HASHES = ArtifactSlice(
    packages=("p-skill",),
    arrange=("apm install p-skill",),
    tampers={
        "disk-or-cache": Tamper(_edit_deployed_skill, lock_paths=(SKILL,)),
        "lock-edited": Tamper(_edit_recorded_skill_hash, lock_paths=(SKILL,)),
        "missing-hash": Tamper(_drop_recorded_skill_hash),
        "upstream-or-proxy": Tamper(_move_skill_upstream),
    },
    cells={
        ("audit-ci", "disk-or-cache"): Expect(
            1,
            (_DRIFT, r"modified: \.agents/skills/p-skill/SKILL\.md"),
            unchanged=_SKILL_STATE,
            smoke=True,
        ),
        ("audit-ci", "lock-edited"): Expect(1, (_DRIFT,), unchanged=_SKILL_STATE),
        # No recorded hash, no drift claim (baseline-checks.md content-integrity;
        # security.md: require_hashes defaults to false).
        ("audit-ci", "missing-hash"): Expect(0, unchanged=_SKILL_STATE),
        # The commit pin holds; audit replays the locked commit from cache.
        ("audit-ci", "upstream-or-proxy"): Expect(0, unchanged=_SKILL_STATE),
        ("frozen", "disk-or-cache"): Expect(
            1, (_FROZEN_HASHES,), unchanged=_SKILL_STATE, smoke=True
        ),
        ("frozen", "lock-edited"): Expect(1, (_FROZEN_HASHES,), unchanged=_SKILL_STATE),
        ("frozen", "missing-hash"): Expect(0, unchanged=_SKILL_STATE),
        ("frozen", "upstream-or-proxy"): Expect(0, unchanged=_SKILL_STATE),
        # Plain install keeps a hand-edited file and warns (drift-detection.md).
        ("install", "disk-or-cache"): Expect(
            0,
            (r"Kept user-edited file \.agents/skills/p-skill/SKILL\.md",),
            unchanged=_SKILL_STATE,
        ),
        # Install re-records the hash of the bytes it deployed.
        ("install", "lock-edited"): Expect(
            0, unchanged=(".agents/skills/p-skill",), check=_skill_hash_matches_disk
        ),
        ("install", "missing-hash"): Expect(
            0, unchanged=(".agents/skills/p-skill",), check=_skill_hash_matches_disk
        ),
        ("install", "upstream-or-proxy"): Expect(0, unchanged=_SKILL_STATE),
        # A file whose bytes do not match its recorded hash, or that has no
        # recorded hash, is not provably APM's: uninstall retains it and exits 1
        # (commands/uninstall/cli.py _abort_if_retained_target_cleanup_paths,
        # integration/cleanup.py expected_hash is None).
        ("uninstall-prune", "disk-or-cache"): Expect(
            1, (_RETAINED, r"Retained user-edited file"), unchanged=_SKILL_STATE
        ),
        ("uninstall-prune", "lock-edited"): Expect(
            1, (_RETAINED, r"Retained user-edited file"), unchanged=_SKILL_STATE
        ),
        ("uninstall-prune", "missing-hash"): Expect(1, (_RETAINED,), unchanged=_SKILL_STATE),
        ("uninstall-prune", "upstream-or-proxy"): Expect(
            0, absent=(SKILL, LOCK), forbids=(_RETAINED,)
        ),
        # Update re-resolves refs only; an unchanged commit is a no-op that
        # neither accepts nor rewrites the local state.
        ("update", "disk-or-cache"): Expect(0, unchanged=_SKILL_STATE),
        ("update", "lock-edited"): Expect(0, unchanged=_SKILL_STATE),
        ("update", "missing-hash"): Expect(0, unchanged=_SKILL_STATE),
        # A moved tag is new content the user asked for (update.md).
        ("update", "upstream-or-proxy"): Expect(
            0, (r"New locked content for 1 dependency",), check=_skill_moved_in
        ),
    },
)


# -- slice: git-content_hash (package content_hash of p-skill from git) ---------
# A download or cache copy that differs from content_hash aborts the install
# (security.md "Verify"; install/sources.py content_hash_mismatch_hint).


def _edit_skill_package_copies(world: World, stack: contextlib.ExitStack) -> None:
    del stack
    copies = _package_copies(world, "SKILL.md")
    assert copies, "no materialized p-skill copy"
    for path in copies:
        _append(path, "\nCopy edit.\n")


def _edit_recorded_content_hash(world: World, stack: contextlib.ExitStack) -> None:
    del stack
    recorded = _dependency(_lock(world), "lifecycle/p-skill")["content_hash"]
    _replace_in_lock(world, recorded, "sha256:" + "2" * 64)


def _drop_recorded_content_hash(world: World, stack: contextlib.ExitStack) -> None:
    del stack
    data = _lock(world)
    del _dependency(data, "lifecycle/p-skill")["content_hash"]
    _write_lock(world, data)


def _content_hash_recorded(world: World) -> None:
    recorded = _dependency(_lock(world), "lifecycle/p-skill").get("content_hash")
    assert recorded and recorded.startswith("sha256:"), recorded


_CONTENT_MISMATCH = r"Content hash mismatch for lifecycle/p-skill"

GIT_CONTENT_HASH = ArtifactSlice(
    packages=("p-skill",),
    arrange=("apm install p-skill",),
    tampers={
        "disk-or-cache": Tamper(_edit_skill_package_copies),
        "lock-edited": Tamper(_edit_recorded_content_hash),
        "missing-hash": Tamper(_drop_recorded_content_hash),
        "upstream-or-proxy": Tamper(_move_skill_upstream),
    },
    cells={
        # The drift replay renders the changed copy and reports the deployed file.
        ("audit-ci", "disk-or-cache"): Expect(
            1, (r"drift\s*\|\s*drift detected",), unchanged=_SKILL_STATE
        ),
        # I3: audit --ci must agree with install --frozen, which refuses this lock.
        ("audit-ci", "lock-edited"): Expect(
            1,
            unchanged=_SKILL_STATE,
            xfail=_pending("audit and frozen agree on content hash"),
        ),
        ("audit-ci", "missing-hash"): Expect(0, unchanged=_SKILL_STATE),
        ("audit-ci", "upstream-or-proxy"): Expect(0, unchanged=_SKILL_STATE),
        ("frozen", "disk-or-cache"): Expect(1, (_CONTENT_MISMATCH,), unchanged=_SKILL_STATE),
        ("frozen", "lock-edited"): Expect(1, (_CONTENT_MISMATCH,), unchanged=_SKILL_STATE),
        # Entries without a content hash are accepted (security.md defaults).
        ("frozen", "missing-hash"): Expect(0, unchanged=_SKILL_STATE),
        ("frozen", "upstream-or-proxy"): Expect(0, unchanged=_SKILL_STATE),
        ("install", "disk-or-cache"): Expect(1, (_CONTENT_MISMATCH,), unchanged=_SKILL_STATE),
        ("install", "lock-edited"): Expect(
            1, (_CONTENT_MISMATCH,), unchanged=_SKILL_STATE, smoke=True
        ),
        # Install records the hash of verified bytes again.
        ("install", "missing-hash"): Expect(
            0, unchanged=(".agents/skills/p-skill",), check=_content_hash_recorded
        ),
        ("install", "upstream-or-proxy"): Expect(0, unchanged=_SKILL_STATE),
        ("uninstall-prune", "disk-or-cache"): Expect(0, absent=(SKILL, LOCK)),
        ("uninstall-prune", "lock-edited"): Expect(0, absent=(SKILL, LOCK)),
        ("uninstall-prune", "missing-hash"): Expect(0, absent=(SKILL, LOCK)),
        ("uninstall-prune", "upstream-or-proxy"): Expect(0, absent=(SKILL, LOCK)),
        # An unchanged commit takes no new content (update.md).
        ("update", "disk-or-cache"): Expect(0, unchanged=_SKILL_STATE),
        ("update", "lock-edited"): Expect(0, unchanged=_SKILL_STATE),
        ("update", "missing-hash"): Expect(0, unchanged=_SKILL_STATE),
        ("update", "upstream-or-proxy"): Expect(
            0, (r"New locked content for 1 dependency",), check=_skill_moved_in
        ),
    },
)


# -- slice: approval-pins (p-exec from git, hooks pinned in apm.yml) -----------
# approve.md "Pinned approvals": a pin that no longer matches parks the item
# (definition-changed); the pin store is apm.yml executables.allow.


def _edit_hook_package_copies(world: World, stack: contextlib.ExitStack) -> None:
    del stack
    copies = _package_copies(world, "session.json")
    assert copies, "no materialized p-exec copy"
    for path in copies:
        path.chmod(0o644)
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("echo p-exec", "echo changed"), encoding="utf-8")


def _edit_hook_pin(world: World, stack: contextlib.ExitStack) -> None:
    del stack
    manifest = world.project / MANIFEST
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    pins = data["executables"]["allow"]["lifecycle/p-exec"]["hooks"]
    assert HOOK_SOURCE in pins, pins
    pins[HOOK_SOURCE] = "sha256:" + "3" * 64
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def _move_hook_upstream(world: World, stack: contextlib.ExitStack) -> None:
    def edit(text: str) -> str:
        return text.replace("echo p-exec", "echo moved")

    stack.enter_context(
        _moved_upstream(world, "lifecycle/p-exec", _edit_files("session.json", edit))
    )


_HOOK_STATE = (LOCK, MANIFEST, ".github/hooks")
_PARKED = r"Parked hook file '\.apm/hooks/session\.json' \(lifecycle/p-exec\): definition changed"
_EXEC_MISMATCH = r"Content hash mismatch for lifecycle/p-exec"

APPROVAL_PINS = ArtifactSlice(
    packages=("p-exec",),
    arrange=("apm install p-exec", "apm approve p-exec", "apm install"),
    tampers={
        "disk-or-cache": Tamper(_edit_hook_package_copies),
        "lock-edited": Tamper(_edit_hook_pin),
        "upstream-or-proxy": Tamper(_move_hook_upstream),
    },
    cells={
        # The replay parks the hook, so the deployed one is reported.
        ("audit-ci", "disk-or-cache"): Expect(
            1, (r"orphaned: \.github/hooks/p-exec-session\.json",), unchanged=_HOOK_STATE
        ),
        ("audit-ci", "lock-edited"): Expect(
            1, (r"orphaned: \.github/hooks/p-exec-session\.json",), unchanged=_HOOK_STATE
        ),
        ("audit-ci", "upstream-or-proxy"): Expect(0, unchanged=_HOOK_STATE),
        ("frozen", "disk-or-cache"): Expect(1, (_EXEC_MISMATCH,), unchanged=_HOOK_STATE),
        # I3: frozen must agree with audit --ci (exit 1) and keep lock and disk in step.
        ("frozen", "lock-edited"): Expect(
            1,
            unchanged=_HOOK_STATE,
            xfail=_pending("frozen and approval pin agreement"),
        ),
        ("frozen", "upstream-or-proxy"): Expect(0, unchanged=_HOOK_STATE),
        ("install", "disk-or-cache"): Expect(1, (_EXEC_MISMATCH,), unchanged=_HOOK_STATE),
        # I4: the item no longer matches its pin, so install retires it.
        ("install", "lock-edited"): Expect(0, (_PARKED,), absent=(HOOK,)),
        ("install", "upstream-or-proxy"): Expect(0, unchanged=_HOOK_STATE),
        ("uninstall-prune", "disk-or-cache"): Expect(0, absent=(HOOK, LOCK)),
        ("uninstall-prune", "lock-edited"): Expect(0, absent=(HOOK, LOCK)),
        ("uninstall-prune", "upstream-or-proxy"): Expect(0, absent=(HOOK, LOCK)),
        # Same commit: the changed copy fails closed (update.md).
        ("update", "disk-or-cache"): Expect(1, (_EXEC_MISMATCH,), unchanged=_HOOK_STATE),
        ("update", "lock-edited"): Expect(
            0, (r"parked pending approval",), absent=(HOOK,), present=(LOCK,)
        ),
        # A moved tag with a changed hook is accepted as content, never as approved.
        ("update", "upstream-or-proxy"): Expect(
            0,
            (r"New locked content for 1 dependency", r"parked pending approval"),
            absent=(HOOK,),
        ),
    },
)


# -- shared: an outside-the-project file every containment row keeps intact -----

_OUTSIDE_TEXT = "outside the project\n"


def _outside(world: World) -> Path:
    """A file outside the project (and outside every snapshot root's managed paths)."""
    path = world.root / "outside" / "sentinel.txt"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_OUTSIDE_TEXT, encoding="ascii")
    return path


def _outside_intact(world: World) -> None:
    assert _outside(world).read_text(encoding="ascii") == _OUTSIDE_TEXT
    escaped = sorted(str(path) for path in world.root.rglob("escape.txt"))
    assert not escaped, escaped


def _link_to_outside(world: World, relative: str) -> None:
    path = world.project / relative
    path.unlink()
    path.symlink_to(_outside(world))


# -- slice: path-containment (p-skill from git) ---------------------------------
# A deployed path that is a symlink out of the project is never written or
# deleted through (install.md "Refusing deployment through a symlinked
# destination"); a package symlink that leaves its checkout is refused at
# download (utils/git_sparse.py, "resolves outside the repository").


def _link_deployed_skill(world: World, stack: contextlib.ExitStack) -> None:
    del stack
    _link_to_outside(world, SKILL)


def _commit_escaping_link(world: World, stack: contextlib.ExitStack) -> None:
    def change(work: Path) -> None:
        skill = next(path for path in work.rglob("SKILL.md") if ".git" not in path.parts)
        link = skill.parent / "notes.md"
        link.symlink_to(os.path.relpath(_outside(world), link.parent))

    stack.enter_context(_moved_upstream(world, "lifecycle/p-skill", change))


PATH_CONTAINMENT = ArtifactSlice(
    packages=("p-skill",),
    arrange=("apm install p-skill",),
    tampers={
        "disk-or-cache": Tamper(_link_deployed_skill, lock_paths=(SKILL,)),
        "upstream-or-proxy": Tamper(_commit_escaping_link),
    },
    cells={
        ("install", "disk-or-cache"): Expect(
            1, (r"outside the project tree",), unchanged=(LOCK, MANIFEST), check=_outside_intact
        ),
        ("update", "disk-or-cache"): Expect(0, unchanged=(LOCK, MANIFEST), check=_outside_intact),
        ("uninstall-prune", "disk-or-cache"): Expect(
            1, (_RETAINED,), unchanged=(LOCK, MANIFEST), check=_outside_intact
        ),
        # The commit pin holds: the moved tag is not fetched.
        ("install", "upstream-or-proxy"): Expect(0, unchanged=_SKILL_STATE, check=_outside_intact),
        ("update", "upstream-or-proxy"): Expect(
            1,
            (r"resolves outside the repository",),
            unchanged=_SKILL_STATE,
            check=_outside_intact,
        ),
        ("uninstall-prune", "upstream-or-proxy"): Expect(
            0, absent=(SKILL, LOCK), check=_outside_intact
        ),
    },
)


# -- local bundle helpers (p-exec packed by the world builder) -------------------

BUNDLE_HOOK = ".github/hooks/p-exec-hooks.json"
_BUNDLE_KEY = re.compile(r'"(p-exec#1\.0\.0@sha256:[0-9a-f]{64})"')
_BUNDLE_STATE = (LOCK, MANIFEST, ".github", ".mcp.json")


def _bundle(world: World) -> Path:
    return Path(world.packages["p-exec:bundle"].spec)


def _rewrite_bundle(world: World, change: Callable[[list], list]) -> None:
    """Rewrite the world's bundle archive in place; *change* maps (ZipInfo, bytes) items."""
    path = _bundle(world)
    with zipfile.ZipFile(path) as archive:
        items = [(info, archive.read(info.filename)) for info in archive.infolist()]
    items = change(items)
    staged = path.with_suffix(".staged")
    with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as archive:
        for info, data in items:
            archive.writestr(info, data)
    staged.replace(path)


def _edit_bundle_files(edit: Callable[[dict], None]) -> Callable[[list], list]:
    """Edit ``pack.bundle_files`` in the bundle's own apm.lock.yaml."""

    def change(items: list) -> list:
        out = []
        for info, data in items:
            if info.filename.endswith("/apm.lock.yaml"):
                document = yaml.safe_load(data)
                edit(document["pack"]["bundle_files"])
                data = yaml.safe_dump(document, sort_keys=False).encode("utf-8")
            out.append((info, data))
        return out

    return change


def _edit_bundle_hook(*, rehash: bool) -> Callable[[list], list]:
    """Change the packed hook command; *rehash* keeps ``bundle_files`` consistent."""

    def change(items: list) -> list:
        out, digest = [], None
        for info, data in items:
            if info.filename.endswith("/hooks.json"):
                data = data.replace(b"echo p-exec", b"echo changed")
                digest = hashlib.sha256(data).hexdigest()
            out.append((info, data))
        assert digest is not None, "bundle has no hooks.json"
        if rehash:
            out = _edit_bundle_files(lambda files: files.__setitem__("hooks.json", digest))(out)
        return out

    return change


def _approve_bundle(world: World, replay: TamperReplay) -> None:
    """Approve the exact bundle the first install named, then install it again.

    The approval key is the bundle digest the install prints (approve.md,
    local bundles); the model learns the approval so I4 stays exact.
    """
    assert replay.last is not None
    found = _BUNDLE_KEY.search(replay.last.stdout + replay.last.stderr)
    assert found, "bundle install printed no approval key"
    manifest = world.project / MANIFEST
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["executables"]["allow"][found.group(1)] = {"hooks": True, "mcp": True}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    replay.model = replay.model.approve("p-exec", replay.kinds["p-exec"])
    replay.step("apm install p-exec")
    assert (world.project / BUNDLE_HOOK).is_file(), "approved bundle hook was not deployed"


def _bundle_slice(tampers: Mapping[str, Tamper], cells: Mapping[tuple[str, str], Expect]):
    return ArtifactSlice(
        packages=("p-exec",),
        arrange=("apm install p-exec",),
        after_arrange=_approve_bundle,
        tampers=tampers,
        cells=cells,
        package_keys={"p-exec": "p-exec:bundle"},
    )


# -- slice: path-containment through a local bundle -----------------------------


def _link_deployed_bundle_hook(world: World, stack: contextlib.ExitStack) -> None:
    del stack
    _link_to_outside(world, BUNDLE_HOOK)


def _bundle_with_parent_entry(world: World, stack: contextlib.ExitStack) -> None:
    del stack

    def change(items: list) -> list:
        root = items[0][0].filename.split("/", 1)[0]
        return [*items, (zipfile.ZipInfo(f"{root}/../../escape.txt"), b"escape\n")]

    _rewrite_bundle(world, change)


BUNDLE_PATH_CONTAINMENT = _bundle_slice(
    tampers={
        "disk-or-cache": Tamper(_link_deployed_bundle_hook, lock_paths=(BUNDLE_HOOK,)),
        "upstream-or-proxy": Tamper(_bundle_with_parent_entry),
    },
    cells={
        ("bundle-install", "disk-or-cache"): Expect(
            1,
            (r"Refusing deployment through a symlinked destination",),
            unchanged=(LOCK, MANIFEST, ".mcp.json"),
            check=_outside_intact,
        ),
        # bundle/local_bundle.py refuses the archive before extraction (usage error, exit 2).
        ("bundle-install", "upstream-or-proxy"): Expect(
            2,
            (r"Refusing to extract path-traversal entry",),
            unchanged=_BUNDLE_STATE,
            check=_outside_intact,
        ),
    },
)


# -- slice: bundle-files (pack.bundle_files of a local bundle) -------------------
# lockfile-spec.md: the pack block is read to verify bundle integrity.

_BUNDLE_INTEGRITY = r"Bundle integrity check failed"


def _bundle_tamper(change: Callable[[list], list]) -> Tamper:
    def apply(world: World, stack: contextlib.ExitStack) -> None:
        del stack
        _rewrite_bundle(world, change)

    return Tamper(apply)


BUNDLE_FILES = _bundle_slice(
    tampers={
        "lock-edited": _bundle_tamper(
            _edit_bundle_files(lambda files: files.__setitem__("hooks.json", "1" * 64))
        ),
        "missing-hash": _bundle_tamper(_edit_bundle_files(lambda files: files.pop("hooks.json"))),
        "upstream-or-proxy": _bundle_tamper(_edit_bundle_hook(rehash=False)),
    },
    cells={
        ("bundle-install", "lock-edited"): Expect(
            1, (_BUNDLE_INTEGRITY, r"Hash mismatch for hooks\.json"), unchanged=_BUNDLE_STATE
        ),
        ("bundle-install", "missing-hash"): Expect(
            1,
            (_BUNDLE_INTEGRITY, r"Unlisted bundle file \(not in pack\.bundle_files\): hooks\.json"),
            unchanged=_BUNDLE_STATE,
        ),
        ("bundle-install", "upstream-or-proxy"): Expect(
            1, (_BUNDLE_INTEGRITY, r"Hash mismatch for hooks\.json"), unchanged=_BUNDLE_STATE
        ),
    },
)


# -- slice: approval-pins through a local bundle ---------------------------------
# A local bundle approval is keyed by the exact bundle digest; changed content
# no longer matches it, so the next install deploys none of its executables.


def _edit_bundle_approval_key(world: World, stack: contextlib.ExitStack) -> None:
    del stack
    manifest = world.project / MANIFEST
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    allow = data["executables"]["allow"]
    key = next(name for name in allow if _BUNDLE_KEY.fullmatch(f'"{name}"'))
    allow[key.split("@sha256:")[0] + "@sha256:" + "3" * 64] = allow.pop(key)
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def _repack_changed_bundle(world: World, stack: contextlib.ExitStack) -> None:
    """Run ``apm pack`` on a changed copy of p-exec and replace the bundle with it."""
    del stack
    source = world.root / "repack" / "p-exec"
    shutil.copytree(world.packages["p-exec"].spec, source)
    _edit_files("session.json", lambda text: text.replace("echo p-exec", "echo changed"))(source)
    output = world.root / "repack" / "out"
    output.mkdir()
    packed = driver.run(replace(world, project=source), ["pack", "--archive", "-o", str(output)])
    assert packed.returncode == 0, packed.stdout + packed.stderr
    (archive,) = output.iterdir()
    shutil.copyfile(archive, _bundle(world))


_BUNDLE_PARKED = r"hooks are executable and not approved for this exact bundle content"
_BUNDLE_RETIRES = _pending("bundle reinstall retires a hook whose approval no longer matches")

BUNDLE_APPROVAL_PINS = _bundle_slice(
    tampers={
        "disk-or-cache": _bundle_tamper(_edit_bundle_hook(rehash=True)),
        "lock-edited": Tamper(_edit_bundle_approval_key),
        "upstream-or-proxy": Tamper(_repack_changed_bundle),
    },
    cells={
        # I4: the deployed hook is no longer approved, so the install removes it.
        ("bundle-install", "disk-or-cache"): Expect(
            0, (_BUNDLE_PARKED,), absent=(BUNDLE_HOOK,), xfail=_BUNDLE_RETIRES
        ),
        ("bundle-install", "lock-edited"): Expect(
            0, (_BUNDLE_PARKED,), absent=(BUNDLE_HOOK,), xfail=_BUNDLE_RETIRES
        ),
        ("bundle-install", "upstream-or-proxy"): Expect(
            0, (_BUNDLE_PARKED,), absent=(BUNDLE_HOOK,), xfail=_BUNDLE_RETIRES
        ),
    },
)


# Artifact id (second field of the obligation id) -> its slices (one per arrangement).
SLICES: dict[str, tuple[ArtifactSlice, ...]] = {
    "deployed_file_hashes": (DEPLOYED_FILE_HASHES,),
    "git-content_hash": (GIT_CONTENT_HASH,),
    "approval-pins": (APPROVAL_PINS, BUNDLE_APPROVAL_PINS),
    "path-containment": (PATH_CONTAINMENT, BUNDLE_PATH_CONTAINMENT),
    "bundle-files": (BUNDLE_FILES,),
}


def _slice_of(artifact: str, op: str, tamper: str) -> ArtifactSlice:
    return next(slice_ for slice_ in SLICES[artifact] if (op, tamper) in slice_.cells)


def _rows():
    for artifact, slices in SLICES.items():
        for (op, tamper), expect in (item for s in slices for item in s.cells.items()):
            cell_id = f"I|{artifact}|{op}|{tamper}"
            marks = []
            if expect.smoke:
                marks.append(pytest.mark.lifecycle_smoke)
            if expect.xfail:
                marks.append(pytest.mark.xfail(strict=True, reason=expect.xfail))
            yield pytest.param(cell_id, id=cell_id, marks=marks)


ROW_IDS = [param.values[0] for param in _rows()]


def _view(world: World, slice_: ArtifactSlice) -> World:
    """The world with each slice package name bound to the slice's PackageRef."""
    packages = dict(world.packages)
    for name in slice_.packages:
        packages[name] = world.packages[slice_.package_keys.get(name, name)]
    return replace(world, packages=packages)


def run_integrity_row(world: World, cell_id: str) -> None:
    """Arrange, tamper, run the op, check I1-I9 and the obligation evidence."""
    _, artifact, op, tamper_id = cell_id.split("|")
    slice_ = _slice_of(artifact, op, tamper_id)
    expect = slice_.cells[(op, tamper_id)]
    tamper = slice_.tampers[tamper_id]
    if slice_.setup is not None:
        slice_.setup(world)
    view = _view(world, slice_)
    row = Row(
        id=f"T7c/integrity/{artifact}",
        promises=("P-OWN-package-content-hash",),
        packages=slice_.packages,
        targets=("copilot",),
        scope="project",
        given=(),
        when=(),
    )
    replay = TamperReplay(view, row)
    replay._set_targets()
    for step in slice_.arrange:
        replay.step(step)
    if slice_.after_arrange is not None:
        slice_.after_arrange(world, replay)
    with contextlib.ExitStack() as stack:
        tamper.apply(world, stack)
        replay.tampered = True
        replay.excused = tamper.lock_paths
        before = replay.observe()
        line = slice_.ops[op](slice_.packages[0])
        replay._run(line, read_only=op in _READ_ONLY_OPS, pinned_exit=expect.exit)
        after = replay.observe()
    if expect.check is not None:
        expect.check(world)
    oracles.check_obligation(
        cell_id,
        strength=STRENGTH,
        before=before,
        after=after,
        result=replay.last,
        evidence=oracles.Evidence(
            exit=expect.exit,
            output_matches=expect.output,
            output_forbids=expect.forbids,
            unchanged={"project": expect.unchanged} if expect.unchanged else {},
            absent={"project": expect.absent} if expect.absent else {},
            present={"project": expect.present} if expect.present else {},
        ),
    )


@pytest.mark.parametrize("cell_id", list(_rows()))
def test_integrity_row(lifecycle_world: World, cell_id: str) -> None:
    run_integrity_row(lifecycle_world, cell_id)


def test_every_row_is_a_generated_integrity_obligation() -> None:
    space = security_obligations()["I"]
    unknown = sorted(set(ROW_IDS) - set(space))
    assert not unknown, f"rows with no I obligation id: {unknown}"
    assert len(ROW_IDS) == len(set(ROW_IDS))
    assert {space[cell_id] for cell_id in ROW_IDS} <= {STRENGTH}
    for artifact, slices in SLICES.items():
        for slice_ in slices:
            assert set(slice_.ops) >= {op for op, _ in slice_.cells}, artifact
            assert set(slice_.tampers) >= {tamper for _, tamper in slice_.cells}, artifact
