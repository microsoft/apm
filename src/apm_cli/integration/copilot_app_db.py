"""GitHub Copilot desktop App SQLite-backed workflow deployment.

The Copilot desktop App stores its scheduled workflows in
``~/.copilot/data.db`` (SQLite, WAL journal mode).  APM deploys prompts
whose frontmatter carries workflow-shape keys (``interval``,
``schedule_hour``, ``schedule_day``) as rows in that ``workflows`` table
so the app surfaces them in its Workflows tab.  ``mode`` / ``model`` /
``reasoning_effort`` remain optional fields on a workflow but are not
shape markers (they overload with plain VSCode / Copilot slash-command
prompts).  This module is the I/O boundary:

1. **Resolution** -- locate ``~/.copilot/data.db`` on the current machine
   (override with ``APM_COPILOT_APP_DB`` for tests or non-standard layouts).

2. **Lockfile translation** -- workflow rows are referenced in
   ``apm.lock.yaml`` via the synthetic ``copilot-app-db://workflows/<id>``
   URI scheme.  ``apm.lock`` always carries the rendered URI; absolute
   filesystem paths never leak into the lockfile.

3. **Schema guard** -- check ``PRAGMA user_version`` before any write.
   Below the minimum tested version we hard-fail (the ``workflows``
   table may genuinely not exist).  Above the maximum tested version
   we warn-and-continue: the Copilot App ships fast and most schema
   bumps are additive and forward-compatible with APM's read/write
   surface.

4. **WAL-safe writes** -- the app keeps a writer connection open while
   running; use ``BEGIN IMMEDIATE`` + bounded retry to coexist without
   blocking the foreground process.

5. **Namespacing** -- every APM-deployed row uses an ``apm--<owner>--
   <pkg-name>--<prompt-name>`` ID so uninstall removes only APM rows and
   never user-authored ones.

Security posture
----------------
There is no application-level authentication on the SQLite file.  The
DB is protected by filesystem permissions alone (``~/.copilot/`` is
0700 on macOS/Linux when created by the app).  Anything that can write
to this file can already exfiltrate the user's tokens.  We document
this transparently; this module does not promise auth it cannot
deliver.

Design note
-----------
Pure-stdlib (``sqlite3`` is in the standard library).  Always importable
but functionally inert until the ``copilot_app`` experimental flag is
enabled by the caller.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COPILOT_APP_URI_SCHEME: str = "copilot-app-db://"
"""Synthetic URI prefix for Copilot App DB rows in lockfile entries."""

COPILOT_APP_LOCKFILE_PREFIX: str = "copilot-app-db://workflows/"
"""Full prefix for workflow entries in the lockfile (scheme + table segment)."""

_DEFAULT_DB_RELATIVE: str = ".copilot/data.db"
"""Relative path from the user's home directory to the Copilot App DB."""

_MIN_SUPPORTED_USER_VERSION: int = 13
"""Lowest ``PRAGMA user_version`` we are tested against.

Empirical: the live app on macOS reports ``user_version = 13`` as of
the design's reverse-engineering pass.  Lower values would imply a
pre-workflows schema where the ``workflows`` table doesn't exist.
"""

_MAX_SUPPORTED_USER_VERSION: int = 15
"""Highest ``PRAGMA user_version`` we are tested against.

When the app ships a newer schema we refuse to write rather than risk
corrupting forward-incompatible columns.  Bump after explicit
re-testing against the new schema.
"""

_WAL_RETRY_TIMEOUT_S: float = 5.0
"""Wall-clock budget for ``BEGIN IMMEDIATE`` retries when the app
holds a long-running write transaction."""

_WAL_RETRY_BACKOFF_S: float = 0.05
"""Initial backoff between ``BEGIN IMMEDIATE`` retries.  Doubles each
attempt up to ``_WAL_RETRY_MAX_BACKOFF_S``."""

_WAL_RETRY_MAX_BACKOFF_S: float = 0.5
"""Cap on individual retry backoff -- prevents runaway sleep."""

_NAMESPACE_PREFIX: str = "apm--"
"""Mandatory prefix on every APM-deployed workflow row's primary key."""

_VALID_INTERVALS: frozenset[str] = frozenset({"manual", "hourly", "daily", "weekly"})
"""App-enforced ``CHECK (interval IN (...))`` constraint mirror."""

_VALID_MODES: frozenset[str] = frozenset({"interactive", "plan"})
"""Modes accepted via the ``copilot-app`` target.

The Copilot App's runtime also defines an ``autopilot`` mode, but APM
intentionally does NOT accept it here: until package signing ships
(v3), a third-party package could declare ``mode: autopilot`` and have
the App auto-run its prompt the moment a user flips the in-App enable
toggle.  Refusing autopilot at the writer is the secure-by-default
behaviour; users who want autopilot can still set it themselves in the
App UI on a per-row basis."""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CopilotAppDbError(Exception):
    """Raised for any Copilot App DB I/O failure with an actionable diagnostic.

    Callers should format ``str(err)`` via ``CommandLogger.error()`` so
    the user sees the message with an ``[x]`` symbol.  Sub-types let
    callers branch on specific failure modes (missing DB vs schema
    mismatch vs lock contention).
    """


class CopilotAppDbMissingError(CopilotAppDbError):
    """The DB file is absent -- the Copilot App is not installed.

    Actionable: install the app, or unset ``--target copilot-app``.
    """


class CopilotAppDbSchemaError(CopilotAppDbError):
    """``PRAGMA user_version`` is outside our tested range.

    Actionable: upgrade APM to a release that supports the new schema.
    """


class CopilotAppDbLockedError(CopilotAppDbError):
    """``BEGIN IMMEDIATE`` exceeded ``_WAL_RETRY_TIMEOUT_S``.

    Actionable: close the Copilot App momentarily and retry, or stop
    whatever else is holding a long-running write.
    """


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_copilot_app_db_path() -> Path | None:
    """Locate the Copilot App SQLite file on the current machine.

    Resolution order:

    1. ``APM_COPILOT_APP_DB`` environment variable (highest priority,
       used by tests).
    2. ``~/.copilot/data.db`` (the only documented install location for
       the desktop app today).

    Returns ``None`` when no DB file exists, signalling target
    unavailability to the resolver.  Callers should treat this as
    "Copilot App not installed" and skip the target (auto-detect) or
    raise an actionable error (explicit ``--target copilot-app``).
    """
    env_override = os.environ.get("APM_COPILOT_APP_DB")
    if env_override:
        candidate = Path(env_override).expanduser()
        return candidate if candidate.is_file() else None

    home_candidate = Path.home() / _DEFAULT_DB_RELATIVE
    return home_candidate if home_candidate.is_file() else None


def resolve_copilot_app_root() -> Path | None:
    """Return ``~/.copilot/`` when the Copilot App DB is present.

    This is the value plugged into the ``copilot-app`` target's
    ``user_root_resolver``: returning ``None`` makes the target invisible
    when the app is not installed, mirroring the cowork pattern.
    """
    db_path = resolve_copilot_app_db_path()
    return db_path.parent if db_path is not None else None


# ---------------------------------------------------------------------------
# Namespacing
# ---------------------------------------------------------------------------


_SLUG_RE: re.Pattern[str] = re.compile(r"[^a-zA-Z0-9_-]+")


def _slugify(token: str) -> str:
    """Reduce *token* to safe ASCII-alphanumeric + hyphen/underscore."""
    return _SLUG_RE.sub("-", token).strip("-").lower() or "unknown"


def namespaced_id(package_owner: str, package_name: str, prompt_name: str) -> str:
    """Return the canonical workflow primary key for an APM-deployed row.

    Format: ``apm--<owner>--<pkg>--<prompt>`` with each segment slugified
    to ``[a-z0-9-]+``.  Used as both the SQL primary key and the trailing
    segment of the lockfile URI.

    The double-hyphen separator is intentional: it is invalid inside a
    GitHub username, package name, or prompt name, so it cannot collide
    with a user-chosen ID.
    """
    return (
        f"{_NAMESPACE_PREFIX}{_slugify(package_owner)}--"
        f"{_slugify(package_name)}--{_slugify(prompt_name)}"
    )


def is_apm_managed_id(workflow_id: str) -> bool:
    """Return True if *workflow_id* uses the APM namespace prefix."""
    return workflow_id.startswith(_NAMESPACE_PREFIX)


# ---------------------------------------------------------------------------
# Lockfile URI translation
# ---------------------------------------------------------------------------


def to_lockfile_uri(workflow_id: str) -> str:
    """Encode a workflow row's primary key as a lockfile URI.

    Returns a string like ``copilot-app-db://workflows/apm--foo--bar--baz``.
    Raises ``ValueError`` if *workflow_id* lacks the APM namespace prefix
    (we only ever record our own rows in the lockfile).
    """
    if not is_apm_managed_id(workflow_id):
        raise ValueError(f"Refusing to lockfile-encode non-APM workflow id: {workflow_id!r}")
    return f"{COPILOT_APP_LOCKFILE_PREFIX}{workflow_id}"


def from_lockfile_uri(lockfile_uri: str) -> str:
    """Decode a ``copilot-app-db://`` lockfile URI to a workflow id.

    Raises ``ValueError`` if the URI does not match our scheme + table or
    if the trailing id is not in the APM namespace.
    """
    if not lockfile_uri.startswith(COPILOT_APP_LOCKFILE_PREFIX):
        raise ValueError(f"Not a copilot-app lockfile URI: {lockfile_uri!r}")
    workflow_id = lockfile_uri[len(COPILOT_APP_LOCKFILE_PREFIX) :]
    if not is_apm_managed_id(workflow_id):
        raise ValueError(f"Refusing to decode non-APM workflow id from lockfile: {workflow_id!r}")
    return workflow_id


def is_copilot_app_uri(lockfile_path: str) -> bool:
    """Return True if *lockfile_path* uses the copilot-app DB scheme."""
    return lockfile_path.startswith(COPILOT_APP_URI_SCHEME)


# ---------------------------------------------------------------------------
# DB connection + version guard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowRow:
    """Subset of the ``workflows`` table columns APM writes.

    Fields not listed here (``created_at``, ``updated_at``,
    ``last_run_at``, ``next_run_at``) are left to the database defaults
    or to existing values when updating. ``project_id`` is now first-
    class on the write path (PR A) so every APM-installed workflow row
    is scoped to a real ``projects`` row.
    """

    id: str
    name: str
    prompt: str
    interval: str = "manual"
    schedule_hour: int = 9
    schedule_day: int = 1
    enabled: int = 0
    model: str | None = None
    reasoning_effort: str | None = None
    mode: str | None = None
    project_id: str | None = None


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a short-lived SQLite connection with WAL-friendly settings.

    * ``isolation_level=None`` -- we drive transactions explicitly via
      ``BEGIN IMMEDIATE`` so the retry loop can observe lock errors at
      the right moment.
    * ``timeout=0`` -- we manage our own retry loop with backoff; the
      built-in timeout would block too long inside a single statement.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=0)
    conn.row_factory = sqlite3.Row
    return conn


_warned_user_versions: set[int] = set()
"""Process-wide dedup set for forward-compat warnings.

``deploy_workflow`` is called once per APM-managed workflow row, so a
single ``apm install`` against a newer-than-tested App schema would
otherwise emit the same warning N times.  Reset by tests via
``_reset_user_version_warning_state``.
"""


def _reset_user_version_warning_state() -> None:
    """Test helper: clear the dedup set between cases."""
    _warned_user_versions.clear()


def _check_user_version(conn: sqlite3.Connection) -> int:
    """Read ``PRAGMA user_version`` and enforce the supported range.

    Returns the version integer.  Raises ``CopilotAppDbSchemaError``
    when the version is *below* ``_MIN_SUPPORTED_USER_VERSION`` (the
    ``workflows`` table may genuinely not exist on a pre-workflows
    schema -- a hard fail is correct).

    When the version is *above* ``_MAX_SUPPORTED_USER_VERSION`` we
    warn-and-continue: the Copilot App ships fast and most user_version
    bumps are additive and forward-compatible with APM's narrow
    read/write surface.  Hard-failing on every bump made every Copilot
    App release a release window for APM.  The warning is emitted once
    per process (deduped by version) via ``_rich_warning`` so multi-row
    installs don't spam.
    """
    cur = conn.execute("PRAGMA user_version")
    version = int(cur.fetchone()[0])
    if version < _MIN_SUPPORTED_USER_VERSION:
        raise CopilotAppDbSchemaError(
            f"Copilot App DB schema is older than supported "
            f"(user_version={version}, need >={_MIN_SUPPORTED_USER_VERSION}). "
            f"Update the GitHub Copilot app to a version that includes "
            f"the workflows feature."
        )
    if version > _MAX_SUPPORTED_USER_VERSION and version not in _warned_user_versions:
        _warned_user_versions.add(version)
        # Wording authored by the devx-ux-expert persona; ship verbatim.
        from apm_cli.utils.console import _rich_warning

        _rich_warning(
            f"[!] Copilot App schema version {version} is newer than APM's "
            f"tested maximum ({_MAX_SUPPORTED_USER_VERSION}). Proceeding "
            f"anyway -- if you see unexpected behavior, run: apm --version "
            f"&& gh issue new -R microsoft/apm -t 'Schema v{version} compat' "
            f"-b 'Observed: <describe>'"
        )
    return version


def _begin_immediate_with_retry(conn: sqlite3.Connection) -> None:
    """Issue ``BEGIN IMMEDIATE`` with bounded exponential backoff.

    The Copilot App keeps a writer connection open while running, so a
    naive ``BEGIN IMMEDIATE`` can collide with an in-flight app write.
    Retry until ``_WAL_RETRY_TIMEOUT_S`` elapses, then raise
    ``CopilotAppDbLockedError`` with an actionable diagnostic.
    """
    deadline = time.monotonic() + _WAL_RETRY_TIMEOUT_S
    backoff = _WAL_RETRY_BACKOFF_S
    last_exc: sqlite3.OperationalError | None = None
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            # 'database is locked' and 'database is busy' are the only
            # transient failure modes for BEGIN IMMEDIATE.  Anything else
            # is a programming error -- let it bubble.
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last_exc = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(min(backoff, _WAL_RETRY_MAX_BACKOFF_S))
            backoff *= 2
    raise CopilotAppDbLockedError(
        f"Copilot App DB stayed locked for {_WAL_RETRY_TIMEOUT_S:.1f}s. "
        f"Close the GitHub Copilot app momentarily and retry, or stop "
        f"any other process writing to ~/.copilot/data.db."
    ) from last_exc


# ---------------------------------------------------------------------------
# Public deploy / cleanup helpers
# ---------------------------------------------------------------------------


def _open_write_txn(db_path: Path) -> sqlite3.Connection:
    """Resolve the App DB and open a write transaction, or raise.

    Centralizes the prelude shared by every writer in this package
    (and by ``copilot_app_project.resolve_or_register_project``):

    1. Refuse if ``db_path`` does not exist (``CopilotAppDbMissingError``).
    2. Open a fresh connection via ``_connect``.
    3. Verify the schema is in range via ``_check_user_version``.
    4. Acquire a write lock via ``_begin_immediate_with_retry``.

    On any failure during steps 3 / 4 the connection is closed before
    the exception propagates. The caller owns the connection on success
    and MUST ``COMMIT`` / ``ROLLBACK`` + ``close()``.

    Extracted to satisfy the repo's R0801 duplicate-code guardrail and
    to keep the missing-DB error wording identical across writers.
    """
    if not db_path.is_file():
        raise CopilotAppDbMissingError(
            f"Copilot App database not found at {db_path}. "
            f"Install the GitHub Copilot desktop app, or omit "
            f"'--target copilot-app'."
        )
    conn = _connect(db_path)
    try:
        _check_user_version(conn)
        _begin_immediate_with_retry(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _validate_row(row: WorkflowRow) -> None:
    """Pre-write sanity check on a ``WorkflowRow`` we are about to store.

    Mirrors the app's ``CHECK`` constraints so we surface bad input as a
    Python-level ``ValueError`` instead of a raw ``sqlite3.IntegrityError``.
    """
    if not is_apm_managed_id(row.id):
        raise ValueError(f"Refusing to write non-APM workflow id: {row.id!r}")
    if row.interval not in _VALID_INTERVALS:
        raise ValueError(
            f"Invalid interval {row.interval!r}; expected one of {sorted(_VALID_INTERVALS)}"
        )
    if row.mode is not None and row.mode not in _VALID_MODES:
        if row.mode == "autopilot":
            raise ValueError(
                "APM does not deploy workflows on autopilot mode -- "
                "a third-party package could otherwise auto-run the moment "
                "the user enables the row.  Users who want autopilot must "
                "set it themselves per-row from the Copilot App UI."
            )
        raise ValueError(f"Invalid mode {row.mode!r}; expected one of {sorted(_VALID_MODES)}")
    if not (0 <= row.schedule_hour <= 23):
        raise ValueError(f"Invalid schedule_hour {row.schedule_hour}; expected 0..23")
    if not (0 <= row.schedule_day <= 6):
        raise ValueError(f"Invalid schedule_day {row.schedule_day}; expected 0..6")
    if row.enabled not in (0, 1):
        raise ValueError(f"Invalid enabled {row.enabled}; expected 0 or 1")


def deploy_workflow(db_path: Path, row: WorkflowRow) -> str:
    """Insert or update a single workflow row owned by APM.

    On INSERT the row arrives with whatever the caller passed.  On
    UPDATE behaviour depends on whether execution-affecting fields
    changed:

    * If ``prompt``, ``mode``, ``interval``, ``schedule_hour``,
      ``schedule_day``, ``model``, or ``reasoning_effort`` differs from
      the row already in the DB, the user's ``enabled`` opt-in is
      revoked (``enabled = 0``) and the App's ``next_run_at`` is
      cleared.  Rationale: the user opted in to a specific prompt body
      and schedule; a content update is a NEW consent surface.
      Preserving ``enabled`` across content changes would be a silent
      malicious-update vector.
    * Otherwise (e.g. only ``name`` changed), ``enabled``,
      ``last_run_at``, and ``next_run_at`` are preserved.

    Returns the lockfile URI for the deployed row.

    Raises:
        CopilotAppDbMissingError: ``db_path`` does not exist.
        CopilotAppDbSchemaError: ``PRAGMA user_version`` is below
            ``_MIN_SUPPORTED_USER_VERSION``.  Versions above
            ``_MAX_SUPPORTED_USER_VERSION`` only emit a warning.
        CopilotAppDbLockedError: write transaction could not be acquired.
        ValueError: ``row`` fails ``_validate_row``.
    """
    _validate_row(row)
    conn = _open_write_txn(db_path)
    try:
        existing = conn.execute(
            """
            SELECT prompt, mode, interval, schedule_hour, schedule_day,
                   model, reasoning_effort
              FROM workflows WHERE id = ?
            """,
            (row.id,),
        ).fetchone()
        if existing is None:
            # INSERT always writes enabled=0 regardless of row.enabled.
            # The user must opt in via the App UI -- a third-party package
            # cannot auto-run on install even if a future caller passes
            # enabled=1 to this writer. Defence in depth alongside the
            # caller-side enforcement in PromptIntegrator.
            conn.execute(
                """
                INSERT INTO workflows (
                    id, name, prompt, model, reasoning_effort,
                    interval, schedule_hour, schedule_day,
                    enabled, mode, project_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    row.id,
                    row.name,
                    row.prompt,
                    row.model,
                    row.reasoning_effort,
                    row.interval,
                    row.schedule_hour,
                    row.schedule_day,
                    row.mode,
                    row.project_id,
                ),
            )
        else:
            execution_changed = (
                existing["prompt"] != row.prompt
                or existing["mode"] != row.mode
                or existing["interval"] != row.interval
                or existing["schedule_hour"] != row.schedule_hour
                or existing["schedule_day"] != row.schedule_day
                or existing["model"] != row.model
                or existing["reasoning_effort"] != row.reasoning_effort
            )
            if execution_changed:
                conn.execute(
                    """
                    UPDATE workflows
                       SET name = ?,
                           prompt = ?,
                           model = ?,
                           reasoning_effort = ?,
                           interval = ?,
                           schedule_hour = ?,
                           schedule_day = ?,
                           mode = ?,
                           project_id = ?,
                           enabled = 0,
                           next_run_at = NULL,
                           updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                     WHERE id = ?
                    """,
                    (
                        row.name,
                        row.prompt,
                        row.model,
                        row.reasoning_effort,
                        row.interval,
                        row.schedule_hour,
                        row.schedule_day,
                        row.mode,
                        row.project_id,
                        row.id,
                    ),
                )
            else:
                # Self-heal pre-PR-A rows: even when nothing else
                # changed, stamp project_id so a NULL left by an
                # older APM install is filled on the next run.
                conn.execute(
                    """
                    UPDATE workflows
                       SET name = ?,
                           project_id = ?,
                           updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                     WHERE id = ?
                    """,
                    (row.name, row.project_id, row.id),
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return to_lockfile_uri(row.id)


def delete_workflows(db_path: Path, workflow_ids: list[str]) -> int:
    """Delete one or more APM-namespaced workflow rows.

    Refuses to delete any id that does not pass ``is_apm_managed_id`` --
    this is the last line of defence against an uninstall removing a
    user-authored row.

    Returns the number of rows actually removed.  Missing ids are
    silently ignored (uninstall is idempotent).
    """
    if not db_path.is_file():
        # Idempotent: if the app/DB is gone, the rows are gone.
        return 0
    for wid in workflow_ids:
        if not is_apm_managed_id(wid):
            raise ValueError(f"Refusing to delete non-APM workflow id: {wid!r}")
    if not workflow_ids:
        return 0

    conn = _connect(db_path)
    removed = 0
    try:
        _check_user_version(conn)
        _begin_immediate_with_retry(conn)
        try:
            placeholders = ",".join("?" for _ in workflow_ids)
            cur = conn.execute(
                f"DELETE FROM workflows WHERE id IN ({placeholders})",  # noqa: S608
                workflow_ids,
            )
            removed = cur.rowcount
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return removed


def list_managed_workflow_ids(db_path: Path) -> list[str]:
    """Return all APM-namespaced workflow ids currently in the DB.

    Read-only; takes no transaction.  Used by drift-detection and by
    ``apm list`` to surface what's actually deployed.  Returns the empty
    list when the DB is missing.
    """
    if not db_path.is_file():
        return []
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT id FROM workflows WHERE id LIKE ? ORDER BY id",
            (f"{_NAMESPACE_PREFIX}%",),
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
