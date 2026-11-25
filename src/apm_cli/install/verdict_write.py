"""The verified-state record's writer, split out of ``install/verdict_record.py``
verbatim (install LOC budget): records a proven no-op's output, or a converged
full install's attestation, under the lifecycle lock (W3d R4, A1, A2, A6).

Module state stays in ``verdict_record``; this module runs only after the CLI
returned, so a replay never imports it.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from apm_cli.install import verdict_record as vr
from apm_cli.install.verdict_record import ATTESTED, PROVEN


def _record_locked(
    argv: list[str], entry: Any, out: vr._Tee, err: vr._Tee, *, attested: bool = False
) -> None:
    """Record under the lifecycle lock, only over the state the run proved.

    The CLI released its lock when it returned: the record is written only while
    no one else can write, and only when apm.yml and the lock are byte-identical
    to the run's entry and to what the engine held its lock over when it proved
    the no-op (W3d R4).
    """
    try:
        from apm_cli.install.locking import acquire_lifecycle_lock

        lock = acquire_lifecycle_lock()
    except Exception as exc:  # busy: the install succeeded, only the record is lost
        vr._trace(f"no record (lock: {type(exc).__name__})")
        return
    try:
        current = vr._read_nofollow(Path.cwd() / "apm.lock.yaml")
        manifest_at_entry, lock_at_entry = vr._ENTRY_FILES or (None, None)
        if (
            current is None
            or not vr._PROVEN_LOCK
            or vr._PROVEN_LOCK[-1] != current
            # A full install writes the lock it attests; a no-op held it unchanged.
            or (lock_at_entry != current and not attested)
            or vr._entry_bytes("apm.yml") != manifest_at_entry
        ):
            vr._trace("no record (apm.yml or the lock changed while the run proved its verdict)")
            return
        _record_after(argv, entry, out, err, attested=attested)
    except OSError as exc:
        vr._trace(f"no record ({type(exc).__name__})")
    finally:
        lock.release()


def _record_after(
    argv: list[str], entry: Any, out: vr._Tee, err: vr._Tee, *, attested: bool = False
) -> None:
    """Record a verified no-op's output, when every input it rests on is provable."""
    from apm_cli.core.lifecycle_scripts import discover_scripts

    if discover_scripts(str(Path.cwd())):
        # Scripts fire on every install; a vr.replay fires none, so never record (W3d R1).
        vr._trace("no record (lifecycle scripts)")
        return
    lock = _committed_lock()
    if lock is None:
        return
    if attested and not _targets_settled(vr._ATTESTED_INSTALL[0]):
        # The run created a root that widens the target set (W3d A1): a proof
        # would deploy there, so this state is not a planner fixed point.
        vr._trace("no record (the target set changed during the install)")
        return
    if attested and not _orphans_settled():
        vr._trace("no record (the lock retains a dependency apm.yml no longer declares)")
        return
    deployed = frozenset(
        rel
        for key, dep in lock.dependencies.items()
        if key != "."
        for rel in getattr(dep, "deployed_file_hashes", None) or {}
    )
    if attested:
        _write_at_entry(entry, argv, "", "", deployed, kind=ATTESTED, provenance=ATTESTED)
        return
    from apm_cli.core.agents_md_compile_record import may_carry

    if may_carry(Path.cwd()):
        vr._trace("no record (a compiled AGENTS.md may carry a Claude rule)")  # #5434
        return
    provenance = ATTESTED if ATTESTED in vr._VERIFIED_NOOP else PROVEN
    out_text, err_text = "".join(out.parts), "".join(err.parts)
    _write_at_entry(entry, argv, out_text, err_text, deployed, provenance=provenance)


# Terminal styling (SGR) is stripped before a line is judged; any other escape
# sequence is not understood, so the line counts as unsettled (W3d A2 allowlist).
_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _unsettled_line(text: str) -> str | None:
    """The first warning or error line that is not a live-rejudged notice (A2)."""
    for raw in text.splitlines():
        line = _SGR.sub("", raw)
        if "\x1b" in line:
            return repr(raw)
        if vr._WARNING_LINE.match(line) and not any(p.match(line) for p in vr._LIVE_NOTICES):
            return line.strip()
    return None


def emitted() -> list[str]:
    """The warning and error messages this run printed, unwrapped (utils/console.py)."""
    import sys

    return list(getattr(sys.modules.get("apm_cli.utils.console"), "EMITTED", ()))


def unsettled_message() -> str | None:
    """The first printed warning or error, unprefixed or wrapped alike, that is not a
    live-rejudged notice (A2): any such message refuses the attestation."""
    for message in emitted():
        head = _SGR.sub("", message.partition("\n")[0]).strip()
        line = head if head.startswith(("[!]", "[x]")) else f"[!] {head}"
        if not any(p.match(line) for p in vr._LIVE_NOTICES):
            return line
    return None


def _orphans_settled() -> bool:
    """The audit's no-orphan baseline passes on the committed state (a frozen install
    may retain a dependency apm.yml dropped; the hit would skip its notice)."""
    from apm_cli.core.scope import manifest_file
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.policy.ci_checks import _check_no_orphans

    root = Path.cwd()
    try:
        lock = LockFile.read(get_lockfile_path(root))
        manifest = APMPackage.from_apm_yml(manifest_file(root))
        return lock is not None and _check_no_orphans(manifest, lock).passed
    except Exception:
        return False


def _targets_settled(announced: str) -> bool:
    """The target line a fresh resolution announces now equals the run's (A1)."""
    from apm_cli.core.scope import manifest_file
    from apm_cli.core.target_detection import format_provenance, resolve_targets
    from apm_cli.models.apm_package import APMPackage, package_target_declaration

    root = Path.cwd()
    try:
        manifest = APMPackage.from_apm_yml(manifest_file(root))
        restriction = package_target_declaration(manifest).restriction()
        return format_provenance(resolve_targets(root, yaml_targets=restriction)) == announced
    except Exception:
        return False


def _committed_lock() -> Any:
    """The lock this run committed (its owner's read), if a hit could prove it."""
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    try:
        lock = LockFile.read(get_lockfile_path(Path.cwd()))
    except Exception as exc:  # the install succeeded; only the record is lost
        vr._trace(f"no record (lockfile: {type(exc).__name__})")
        return None
    if lock is None:
        vr._trace("no record (no lockfile)")
        return None
    unprovable = lock.local_deployed_files or lock.local_staged_files
    for key, dep in lock.dependencies.items():
        if key != "." and not vr._provable_row(lambda name, dep=dep: getattr(dep, name, None)):
            unprovable = True
    if unprovable:
        vr._trace("no record (content a hit cannot prove)")
        return None
    return lock


def _write_at_entry(
    entry: tuple[dict[str, str], bytes],
    argv: list[str],
    out: str,
    err: str,
    deployed: frozenset[str],
    *,
    kind: str = "output",
    provenance: str = PROVEN,
) -> None:
    """Write the record keyed by the run's entry state (what a hit will see)."""
    vr._ENTRY_ENV, vr._ENTRY_PRESENTATION = entry
    try:
        _write(argv, out, err, deployed, kind=kind, provenance=provenance)
    finally:
        vr._ENTRY_ENV, vr._ENTRY_PRESENTATION = None, None


def _write(
    argv: list[str],
    stdout_text: str,
    stderr_text: str,
    deployed: frozenset[str],
    *,
    kind: str = "output",
    provenance: str = PROVEN,
) -> None:
    root = Path.cwd()
    try:
        if not vr._location_trusted(root):
            vr._trace("no record (APM_HOME inside the project or cache)")
            return  # never create a key or record inside the project or cache
        lock_text = vr._read_nofollow(root / "apm.lock.yaml")
        if lock_text is None:
            vr._trace("no record (no lockfile)")
            return
        from apm_cli.install import verdict_policy

        if kind != ATTESTED:
            stdout_text, stamps = vr._ELAPSED_RE.subn(rf"\g<1>{vr._ELAPSED}\g<2>", stdout_text)
            templated = verdict_policy._template_ages(stdout_text)
            if templated is None:
                vr._trace("no record (a policy age without one cache entry)")
                return
            stdout_text = templated
            if stamps != 1 or vr._CLOCK_RE.search(stdout_text) or vr._CLOCK_RE.search(stderr_text):
                vr._trace("no record (clock-dependent output)")
                return  # O1: only the elapsed seconds may depend on the clock
        if verdict_policy._discovery_unproven(root, vr._policy_identity(root)):
            vr._trace("no record (discovery would fetch a policy candidate)")
            return
        from apm_cli.install import verdict_inputs
        from apm_cli.update_notice import NEW_VERSION_NOTICE

        notice, due = verdict_inputs.notice_schedule(
            vr._ENTRY_TIME[0] if vr._ENTRY_TIME else 0.0,
            output=kind != ATTESTED,
            notified=NEW_VERSION_NOTICE in stdout_text + stderr_text
            or any(NEW_VERSION_NOTICE in message for message in emitted()),
        )
        key = vr._key(create=True)
        if key is None:
            vr._trace("no record (no private key)")
            return
        run_env = sorted(
            name
            for name in vr._ENV_READS
            if isinstance(name, str)
            and name not in vr.ENV_ALLOWLIST
            and name not in vr.ENV_NOT_VERDICT
        )
        surfaces = sorted({*vr._owner_surfaces(root), *verdict_inputs.tls_surfaces(vr._ENTRY_ENV)})
        from apm_cli.install import verdict_git, verdict_policy

        git_system = verdict_git._git_system()
        digest = vr._Digest(key)
        vr._inputs(digest, root, argv, lock_text, run_env, surfaces, git_system, deployed, notice)
        stderr_kept = "".join(
            line
            for line in stderr_text.splitlines(keepends=True)
            if "install fast path:" not in line and "verdict record:" not in line
        )
        issued = time.time()
        record = {
            "v": vr.RECORD_VERSION,
            "stdout": stdout_text,
            "stderr": stderr_kept,
            "exit": 0,
            "issued_at": issued,
            "expires_at": min(verdict_policy._expiry(issued), due or float("inf")),
            "run_env": run_env,
            "surfaces": surfaces,
            "git_system": git_system,
            "notice": notice,
            "kind": kind,
            "provenance": provenance,
        }
        vr._body(digest, record)
        record["mac"] = digest.hexdigest()
        record["tags"] = digest.tags
        import tempfile

        path = vr._record_path(root)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".record-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        vr._trace("recorded" if kind != ATTESTED else "attested")
    except (OSError, ValueError, TypeError) as exc:
        vr._trace(f"no record ({type(exc).__name__})")
        return
