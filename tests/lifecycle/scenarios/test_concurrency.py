"""Two writers on the lifecycle lock: serialization at project and user scope (#4106).

PROMISE: P-OWN-cross-process-lifecycle-serialization -- every state-mutating
APM command takes the cross-process lifecycle lock before its first state
read, so a second writer waits for the first (it prints that it is waiting)
and both effects land: no lost manifest update, no torn lockfile, the lock is
released afterwards (source: .apm/architecture/owners/install-deployment.json,
src/apm_cli/install/locking.py).
PROMISE: P-INV-I1..I9 -- the laws hold for each writer and after the pair.

The holder is always an ``install``. It parks right after it acquires the
lifecycle lock until the test releases it: with the engine driver a one-shot
``sitecustomize`` wraps ``acquire_lifecycle_lock``; with the binary driver a
one-shot ``git`` shim parks the first ``git remote get-url origin`` (policy
discovery under the lock; project scope only). The overlap is deterministic:
the contender starts while the holder owns the lock, must still be running
``_BLOCKED_WINDOW`` seconds after its imports (the shim sees its
``git version``), and must report that it waited. The contenders cover install, update, uninstall,
approve, deny and a self-update setting (``config set self-update.channel``,
the user-config write ``apm self-update`` reads; ``self-update`` itself
replaces the binary over the network and holds no lifecycle lock).

Each writer's I1-I9 check sees the state before and after the pair; its write
set is the union of both declared write sets, and its reference model is the
one in force once the last reconciling writer has finished.

CELL: OP={install,update,uninstall,approve-deny,config} SRC=local-path
      PRIM={skill,hook,mcp} SCOPE={project,user} CONCURRENCY=two-writers
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.model.expected import expected_exit
from tests.lifecycle.oracles import check_cross_cutting
from tests.lifecycle.scenarios._kit import Known, Scenario, Step, StepModel
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-OWN-cross-process-lifecycle-serialization",
        "P-INV-I1",
        "P-INV-I2",
        "P-INV-I3",
        "P-INV-I4",
        "P-INV-I5",
        "P-INV-I7",
        "P-INV-I9",
    ),
]

_WAITING = "Another APM operation is running; waiting up to"
# The contender must still be waiting this long after its imports finished.
_BLOCKED_WINDOW = 1.5
_PARK_TIMEOUT = 60.0
_RECONCILING = frozenset({"install", "update"})


@dataclass(frozen=True)
class Writer:
    line: str
    scope: str
    # HOME paths this writer may write besides receipts and bookkeeping.
    home_writes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PairRow:
    id: str
    scope: str
    given: tuple[str, ...]
    holder: str
    contender: Writer
    then: Callable[[Scenario], None] = lambda scenario: None
    packages: tuple[str, ...] = ("p-skill", "p-child", "p-exec")
    # Strict known cells of the repeated (converged) install after the pair.
    repeat_known: dict[str, Known] = field(default_factory=dict)


# -- the parked holder ----------------------------------------------------------------


# Engine driver: the first process to take the lifecycle lock parks while holding it.
_SITECUSTOMIZE = """\
import os
import time

from apm_cli.install import locking

_acquire = locking.acquire_lifecycle_lock


def _parked_acquire(*args, **kwargs):
    handle = _acquire(*args, **kwargs)
    armed = {armed!r}
    try:
        os.rename(armed, armed + ".taken")
    except OSError:
        return handle
    open({ready!r}, "w").close()
    deadline = time.monotonic() + {timeout!r}
    while not os.path.exists({release!r}) and time.monotonic() < deadline:
        time.sleep(0.05)
    return handle


locking.acquire_lifecycle_lock = _parked_acquire
"""


class _Park:
    """Park the holder inside the lifecycle lock until a release file appears.

    A one-shot hook parks the first lock holder: the lock acquire itself
    (engine driver) or ``git remote get-url`` (binary driver). A ``git`` shim
    marks the contender's start (GitPython's ``git version`` at import).
    """

    def __init__(self, world) -> None:
        self.world = world
        self.dir = world.root / "park-shim"
        self.dir.mkdir()
        self.armed = self.dir / "armed"
        self.ready = self.dir / "ready"
        self.release = self.dir / "release"
        self.started = self.dir / "contender-started"
        park_in_git = driver.selected_driver() == "binary"
        real = shutil.which("git", path=world.env["PATH"])
        script = (
            "#!/bin/sh\n"
            # GitPython runs `git version` while apm imports, before the lock.
            f'if [ "$1" = "version" ] && [ -e "{self.ready}" ]; then : > "{self.started}"; fi\n'
        )
        if park_in_git:
            script += (
                f'if [ "$1 $2" = "remote get-url" ] && mv "{self.armed}" "{self.armed}.taken" '
                "2>/dev/null; then\n"
                f'  : > "{self.ready}"\n'
                "  i=0\n"
                f'  while [ ! -e "{self.release}" ] && [ "$i" -lt 1200 ]; do sleep 0.05; i=$((i+1)); done\n'
                "fi\n"
            )
        script += f'exec "{real}" "$@"\n'
        (self.dir / "git").write_text(script, encoding="ascii")
        (self.dir / "git").chmod(0o755)
        self.saved_env = dict(world.env)
        world.env["PATH"] = os.pathsep.join((str(self.dir), world.env["PATH"]))
        if not park_in_git:
            hook = self.dir / "site"
            hook.mkdir()
            (hook / "sitecustomize.py").write_text(
                _SITECUSTOMIZE.format(
                    armed=str(self.armed),
                    ready=str(self.ready),
                    release=str(self.release),
                    timeout=_PARK_TIMEOUT,
                ),
                encoding="ascii",
            )
            world.env["PYTHONPATH"] = os.pathsep.join(
                p for p in (str(hook), world.env.get("PYTHONPATH", "")) if p
            )
        self.armed.write_text("", encoding="ascii")

    def wait_parked(self, holder: _Run) -> None:
        deadline = time.monotonic() + _PARK_TIMEOUT
        while not self.ready.exists():
            assert holder.thread.is_alive(), f"holder finished before parking: {holder.output()}"
            assert time.monotonic() < deadline, "holder never reached the lifecycle lock"
            time.sleep(0.02)

    def wait_contender(self, contender: _Run) -> None:
        """Wait until the contender is past its imports, then let it reach the lock."""
        deadline = time.monotonic() + _PARK_TIMEOUT
        while not self.started.exists():
            assert contender.thread.is_alive(), f"contender never started: {contender.output()}"
            assert time.monotonic() < deadline, "contender never started"
            time.sleep(0.02)
        contender.thread.join(timeout=_BLOCKED_WINDOW)

    def unpark(self) -> None:
        self.release.write_text("", encoding="ascii")

    def close(self) -> None:
        self.release.write_text("", encoding="ascii")
        self.world.env.clear()
        self.world.env.update(self.saved_env)


class _Run:
    """One driver.run in a thread."""

    def __init__(self, world, args: tuple[str, ...], scope: str) -> None:
        self.result: CommandResult | None = None
        self.error: BaseException | None = None

        def target() -> None:
            try:
                self.result = driver.run(world, args, scope=scope)
            except BaseException as exc:  # surfaced by join()
                self.error = exc

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()

    def join(self) -> CommandResult:
        self.thread.join(timeout=driver.TIMEOUT_SECONDS + 30)
        assert not self.thread.is_alive(), "writer did not finish"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def output(self) -> str:
        if self.result is None:
            return repr(self.error)
        return f"{self.result.stdout}\n{self.result.stderr}"


# -- the pair step --------------------------------------------------------------------


def _args(scenario: Scenario, writer: Writer) -> tuple[str, ...]:
    args = driver.command_args(writer.line, scope=writer.scope)
    return tuple(scenario.specs.get(arg, arg) for arg in args)


def _plan(scenario: Scenario, model, roots, args: tuple[str, ...]):
    """(requested model, model after the command, declared roots after it) from *model*.

    Uses ``Scenario.requested`` and ``Scenario.planned_roots`` for one writer of
    the pair, which the kit's sequential ``run`` cannot drive.
    """
    requested = scenario.requested(args, model=model, roots=roots)
    roots = scenario.planned_roots(args, roots)
    advanced = requested.installed() if args[0] in _RECONCILING else requested
    return requested, advanced, frozenset(roots)


def _check(scenario, writer, args, before, after, result, model, others_home) -> None:
    op = scenario.operation(args)
    if "home" in op.forbidden:
        allowed = dict(op.allowed)
        allowed["home"] = (*allowed.get("home", ()), *writer.home_writes, *others_home)
        op = replace(op, allowed=allowed)
    step_model = StepModel(model, scenario.kinds, False, {}, scenario.expected_seen)
    check_cross_cutting(before, after, result, step_model, op)


def run_pair(scenario: Scenario, holder: Writer, contender: Writer) -> tuple[Step, Step]:
    """Park *holder* inside the lifecycle lock, start *contender*, then release."""
    world = scenario.world
    holder_args, contender_args = _args(scenario, holder), _args(scenario, contender)
    req_holder, after_holder, holder_roots = _plan(
        scenario, scenario.model, scenario.roots, holder_args
    )
    req_contender, final, final_roots = _plan(scenario, after_holder, holder_roots, contender_args)

    before = scenario.observe()
    park = _Park(world)
    try:
        first = _Run(world, holder_args, holder.scope)
        park.wait_parked(first)
        second = _Run(world, contender_args, contender.scope)
        park.wait_contender(second)
        blocked = second.thread.is_alive()
        park.unpark()
        holder_result, contender_result = first.join(), second.join()
    finally:
        park.close()
    after = scenario.observe()

    # Serialization: the contender waited for the holder and said so.
    assert blocked, f"contender finished while the holder held the lock:\n{second.output()}"
    assert _WAITING in contender_result.stdout + contender_result.stderr, second.output()
    assert _WAITING not in holder_result.stdout + holder_result.stderr, first.output()
    for args, result, requested in (
        (holder_args, holder_result, req_holder),
        (contender_args, contender_result, req_contender),
    ):
        allowed_exit = expected_exit(args[0], requested)
        assert result.returncode in allowed_exit, (
            f"R: {' '.join(args)!r} exited {result.returncode}, expected {sorted(allowed_exit)}\n"
            f"{result.stdout}\n{result.stderr}"
        )

    holder_model = final if contender_args[0] in _RECONCILING else after_holder
    _check(
        scenario,
        holder,
        holder_args,
        before,
        after,
        holder_result,
        holder_model,
        contender.home_writes,
    )
    _check(scenario, contender, contender_args, before, after, contender_result, final, ())
    scenario.model, scenario.roots = final, final_roots
    steps = (
        Step(" ".join(holder_args), holder_result, before, after),
        Step(" ".join(contender_args), contender_result, before, after),
    )
    scenario.steps.extend(steps)
    return steps


# -- durable outcome checks -----------------------------------------------------------


def _assert_not_torn(scenario: Scenario) -> None:
    """The manifest and lockfile parse and agree on the declared set."""
    manifest = yaml.safe_load((scenario.manifest_root / "apm.yml").read_text(encoding="utf-8"))
    declared = {str(item) for item in manifest["dependencies"]["apm"] or ()}
    wanted = {scenario.specs[name] for name in scenario.model.declared}
    assert declared == wanted, (
        f"manifest declares {sorted(declared)}, writers asked {sorted(wanted)}"
    )
    assert len(scenario.lock_names()) == len(wanted), sorted(scenario.lock_names())


def _channel_saved(scenario: Scenario) -> None:
    config = json.loads((scenario.world.apm_home / "config.json").read_text(encoding="utf-8"))
    assert config.get("self_update_channel") == "prerelease", config


def _decision(name: str, block: str) -> Callable[[Scenario], None]:
    """The contender's decision landed in ``executables.<block>`` alone (keys may pin a version)."""

    def check(scenario: Scenario) -> None:
        manifest = yaml.safe_load((scenario.manifest_root / "apm.yml").read_text(encoding="utf-8"))
        executables = manifest.get("executables") or {}
        spec = scenario.specs[name]
        holders = {
            key
            for key in ("allow", "deny")
            if any(entry.split("#", 1)[0] == spec for entry in executables.get(key) or {})
        }
        assert holders == {block}, executables

    return check


_SELF_UPDATE = Writer("config set self-update.channel prerelease", "project", (".apm/config.json",))
ROWS = (
    PairRow(
        "install-vs-install", "project", (), "install p-skill", Writer("install p-child", "project")
    ),
    PairRow(
        "install-vs-update",
        "project",
        ("install p-skill",),
        "install p-child",
        Writer("update --yes", "project"),
    ),
    PairRow(
        "install-vs-uninstall",
        "project",
        ("install p-skill",),
        "install p-child",
        Writer("uninstall p-skill", "project"),
    ),
    PairRow(
        "install-vs-approve",
        "project",
        ("install p-exec",),
        "install p-child",
        Writer("approve p-exec", "project"),
        then=_decision("p-exec", "allow"),
    ),
    PairRow(
        "install-vs-deny",
        "project",
        ("install p-exec", "approve p-exec", "install"),
        "install p-child",
        Writer("deny p-exec", "project"),
        then=_decision("p-exec", "deny"),
        # Retiring the last hook removes .github; the repeat recreates it.
        repeat_known={
            "I2": Known("#4132", r"converged install rewrote files: \['project:\.github'\]")
        },
    ),
    PairRow(
        "install-vs-self-update-setting",
        "project",
        (),
        "install p-child",
        _SELF_UPDATE,
        then=_channel_saved,
    ),
    PairRow(
        "user-install-vs-install",
        "user",
        (),
        "install p-skill",
        Writer("install p-child", "user"),
        packages=("p-skill", "p-child"),
    ),
    PairRow(
        "user-install-vs-uninstall",
        "user",
        ("install p-skill",),
        "install p-child",
        Writer("uninstall p-skill", "user"),
        packages=("p-skill", "p-child"),
    ),
    PairRow(
        "user-install-vs-self-update-setting",
        "user",
        (),
        "install p-child",
        _SELF_UPDATE,
        then=_channel_saved,
        packages=("p-skill", "p-child"),
    ),
)


@pytest.mark.parametrize("row", ROWS, ids=[row.id for row in ROWS])
def test_two_writers_serialize_on_the_lifecycle_lock(lifecycle_world, row: PairRow) -> None:
    scenario = Scenario(lifecycle_world, row.packages, scope=row.scope)
    for line in row.given:
        scenario.run(line)

    run_pair(scenario, Writer(row.holder, row.scope), row.contender)

    _assert_not_torn(scenario)
    row.then(scenario)
    # The lock is released: the next writer neither waits nor sees a busy lock.
    converged = scenario.run("install")
    assert _WAITING not in converged.output, converged.output
    scenario.run("install", known=row.repeat_known)  # converged: I2 byte no-op
    if row.scope == "project":
        scenario.pair()
    scenario.assert_deployed()
    scenario.finish()
