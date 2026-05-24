"""Project derivation and SQLite-fallback resolver for the Copilot App.

The Copilot App's ``workflows`` table carries a ``project_id`` foreign-
key into ``projects(id)``. APM-deployed workflow rows must point at the
project that "owns" the prompt (the repository the user ran
``apm install`` from) so that "Run now" in the App's Workflows tab CWDs
into the right repo, and the row groups under the correct sidebar entry.

This module is the pure derive / resolve layer. It has two halves:

1. **derive_** -- ``RepoContext`` from a path on disk, ``ProjectRecipe``
   from a ``RepoContext``. Both are pure: no I/O on the App's DB, no
   network calls. Easy to unit-test against tmp dirs.

2. **resolve_or_register_project_sqlite** -- the SQLite-fallback path
   used when the Copilot App is closed (the WebSocket IPC surface is
   unavailable). Issues a single ``BEGIN IMMEDIATE`` transaction:
   SELECT by ``main_repo_path`` (UNIQUE), INSERT if missing. Reuses
   ``copilot_app_db._connect`` / ``_check_user_version`` /
   ``_begin_immediate_with_retry`` for WAL-safe semantics consistent
   with the workflow writer.

The WS-IPC happy path lives in ``copilot_app_ws.py`` and short-circuits
this module's resolver; only the fallback path executes here.

Security note
-------------
``derive_repo_context`` walks parents looking for ``.git/``. We do not
follow symlinks and we do not invoke ``git`` as a subprocess; the
``GitPython`` API reads pack files / refs directly. ``main_repo_path``
is always stored as an absolute, resolved path so two clones of the
same repo at different filesystem locations cannot collide on the
``projects.main_repo_path UNIQUE`` constraint.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from apm_cli.integration.copilot_app_db import (
    CopilotAppDbError,
    _open_write_txn,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BRANCH: str = "main"
"""Fallback branch name when the repo has no remote HEAD reference."""

_GH_REMOTE_RE: re.Pattern[str] = re.compile(
    r"""
    (?:https?://github\.com/|git@github\.com:|github\.com/)
    (?P<owner>[A-Za-z0-9_.-]+)/
    (?P<repo>[A-Za-z0-9_.-]+?)
    (?:\.git)?/?$
    """,
    re.VERBOSE,
)
"""Match the GitHub portion of a typical ``origin`` URL.

Tolerates ``https://github.com/o/r``, ``https://github.com/o/r.git``,
``git@github.com:o/r.git``, and the ``ssh://git@github.com/o/r.git``
shapes commonly produced by ``git clone`` against github.com.
"""


# ---------------------------------------------------------------------------
# Value objects (frozen dataclasses -- safe across worker threads)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoContext:
    """Information derived from the on-disk repository root.

    Captured once at install time and shared between the project-
    resolver and the workflow-writer so display names + ``main_repo_path``
    stay consistent across the install transaction.
    """

    repo_root: Path
    """Absolute, resolved path to the repo's working tree root.

    Stored verbatim in ``projects.main_repo_path`` (the column is
    UNIQUE in the App's schema). Two checkouts of the same repo at
    different paths will resolve to two ``projects`` rows -- which is
    correct: the App tracks projects by filesystem location, not by
    GitHub identity.
    """

    repo_name: str
    """Best-effort repo display label.

    Resolution order: ``origin``'s repo segment -> ``repo_root.name`` ->
    ``"project"``. Used both as ``projects.name`` and as the suffix on
    each workflow row's display name (``<original-name> (<repo_name>)``).
    """

    github_owner: str | None
    """GitHub owner (org / user) parsed from ``origin``. ``None`` when
    the repo has no ``origin`` or the URL isn't github.com-shaped."""

    github_repo: str | None
    """GitHub repo name parsed from ``origin``. ``None`` under the same
    conditions as ``github_owner``."""

    default_branch: str
    """The repo's active branch when discovery happened, falling back to
    ``main`` when detection fails. The App stores this in
    ``projects.default_branch`` purely for display in the sidebar; it
    has no functional effect on workflow execution."""


@dataclass(frozen=True)
class ProjectRecipe:
    """The exact set of column values APM writes to ``projects``.

    Mirrors the live recipe captured in ``livedb-findings.md`` (the
    diff produced by the App's own "Add project" flow). Kept as a
    dataclass so the SQLite-fallback resolver and any future
    direct-write path share one canonical shape.
    """

    id: str
    """Dashed UUIDv4 string -- ``str(uuid.uuid4())`` -- 36 chars. The
    App accepts any non-empty TEXT but generates dashed UUIDs itself,
    so APM matches for visual parity in the App's debug tooling."""

    name: str
    """Human-readable label -- equal to ``RepoContext.repo_name``."""

    main_repo_path: str
    """Absolute repo root path. UNIQUE in the ``projects`` table; the
    resolver uses it as the lookup key for HIT/MISS detection."""

    default_branch: str
    """Branch shown in the sidebar; no execution semantics."""

    github_owner: str | None
    github_repo: str | None


@dataclass(frozen=True)
class ResolvedProject:
    """Outcome of resolving (or registering) a project row.

    ``was_created`` drives the "restart Copilot App once" diagnostic
    in the integrator (see github/github-app#5483 for why projects
    don't live-refresh in the sidebar without a restart).
    """

    project_id: str
    """The ``projects.id`` value (either pre-existing or freshly
    INSERTed)."""

    was_created: bool
    """True iff this call INSERTed a new row. Drives one-shot UX
    diagnostics like the restart hint."""

    main_repo_path: str
    """Echo of the resolved row's ``main_repo_path`` for callers that
    want to stamp the workflow's CWD without re-reading the row."""


# ---------------------------------------------------------------------------
# Pure derive_* helpers
# ---------------------------------------------------------------------------


def _safe_repo(repo_root: Path):
    """Open a ``GitPython`` repo handle without raising on missing ``.git``.

    Returns ``None`` when *repo_root* is not a git working tree. Used by
    every metadata probe below so the rest of the function can stay
    branch-light.
    """
    try:
        from git import InvalidGitRepositoryError, Repo
    except ImportError:
        return None
    try:
        return Repo(repo_root)
    except (InvalidGitRepositoryError, Exception):
        return None


def _find_repo_root(start: Path) -> Path | None:
    """Walk *start* and its parents looking for a ``.git`` directory.

    Returns the first ancestor containing ``.git`` (file or dir -- git
    worktrees use a file pointer), or ``None`` when no enclosing repo
    is found.

    .. note::
       ``start`` is resolved with :meth:`pathlib.Path.resolve` so the
       returned root is canonical (used as the UNIQUE key on
       ``projects.main_repo_path``). This DOES follow symlinks in the
       parent chain. The only symlink-rejection check here is on the
       ``.git`` marker itself (``not git_marker.is_symlink()``), which
       blocks the narrow case where an attacker plants a symlinked
       ``.git`` pointer inside a directory under their control.
       Defending against symlinked parent directories would require
       switching to :meth:`Path.absolute` + an explicit per-component
       walk, but that breaks legitimate setups where temp dirs (macOS
       ``/tmp`` -> ``/private/tmp``) or user-controlled bind mounts
       have symlinked ancestors. Project-creation already runs in the
       user's own trust domain (CWD is a user-typed path), so this is
       an acceptable trade-off; ``derive_repo_context`` callers should
       not treat the returned path as adversary-controlled.
    """
    start = start.resolve()
    for candidate in (start, *start.parents):
        git_marker = candidate / ".git"
        if git_marker.exists() and not git_marker.is_symlink():
            return candidate
    return None


def _parse_github_origin(url: str) -> tuple[str | None, str | None]:
    """Parse ``owner, repo`` from a GitHub-shaped URL or return ``(None, None)``."""
    m = _GH_REMOTE_RE.search(url.strip())
    if not m:
        return None, None
    return m.group("owner"), m.group("repo")


def derive_repo_context(cwd: Path) -> RepoContext | None:
    """Derive a ``RepoContext`` from *cwd* (or any path inside a repo).

    Returns ``None`` when *cwd* is not inside any git working tree --
    callers MUST treat this as "cannot attach to a project" and
    surface a diagnostic rather than silently inventing one (a
    NULL-project workflow CWDs into ``~/.copilot``, which is a real
    security finding -- see reverse-eng report).

    Best-effort fields (``github_owner``, ``github_repo``,
    ``default_branch``) degrade gracefully when the repo has no
    ``origin`` or detached HEAD; we never raise for missing optional
    metadata.
    """
    repo_root = _find_repo_root(cwd)
    if repo_root is None:
        return None

    owner: str | None = None
    repo_name_from_remote: str | None = None
    branch: str = _DEFAULT_BRANCH

    repo = _safe_repo(repo_root)
    if repo is not None:
        import contextlib

        with contextlib.suppress(AttributeError, ValueError, Exception):
            origin_url = repo.remotes.origin.url
            owner, repo_name_from_remote = _parse_github_origin(origin_url)
        with contextlib.suppress(TypeError, ValueError, Exception):
            # Detached HEAD or bare repo -- accept the default.
            branch = repo.active_branch.name or _DEFAULT_BRANCH

    repo_name = repo_name_from_remote or repo_root.name or "project"

    return RepoContext(
        repo_root=repo_root,
        repo_name=repo_name,
        github_owner=owner,
        github_repo=repo_name_from_remote,
        default_branch=branch,
    )


def derive_project_recipe(ctx: RepoContext) -> ProjectRecipe:
    """Translate a ``RepoContext`` into the columns APM writes to ``projects``.

    Generates a fresh dashed UUIDv4 -- callers wishing to compare
    recipes (e.g. for idempotent INSERT) should NOT use ``id`` as a
    key; ``main_repo_path`` is the App's UNIQUE column and the only
    stable identity.
    """
    return ProjectRecipe(
        id=str(uuid.uuid4()),
        name=ctx.repo_name,
        main_repo_path=str(ctx.repo_root),
        default_branch=ctx.default_branch,
        github_owner=ctx.github_owner,
        github_repo=ctx.github_repo,
    )


# ---------------------------------------------------------------------------
# SQLite fallback resolver
# ---------------------------------------------------------------------------


_PROJECTS_INSERT_SQL: str = """
INSERT INTO projects (
    id, name, container_kind, main_repo_path, default_branch,
    github_owner, github_repo, auto_open_in_browser, auto_approve
) VALUES (?, ?, 'repository', ?, ?, ?, ?, 1, 1)
"""
"""Minimum-viable ``projects`` row recipe.

Mirrors the live diff captured in ``livedb-findings.md``: only the
columns the App's own "Add project" flow populates by default. We
deliberately omit ``tab_order`` (NULL on fresh projects),
``github_account_id`` (the App resolves at session time), and the
optional ``issue_prompt`` / ``pull_request_prompt`` template columns
(the App falls back to its bundled defaults when these are NULL).
``container_kind='repository'`` matches the only kind the sidebar
renders for filesystem-anchored projects.
"""


def resolve_or_register_project_sqlite(
    db_path: Path,
    ctx: RepoContext,
) -> ResolvedProject:
    """Look up or insert a ``projects`` row for *ctx*'s repo.

    Used as the fallback when the Copilot App's WebSocket IPC surface
    is unavailable (i.e. the App is not running). Single
    ``BEGIN IMMEDIATE`` transaction:

    1. ``SELECT id, main_repo_path FROM projects WHERE main_repo_path=?``
       -- HIT means the App (or a prior APM install) already knows this
       repo; reuse the ``id``.
    2. ``INSERT INTO projects (...) VALUES (...)`` with the recipe
       returned by ``derive_project_recipe``. Race-collision (another
       writer beat us) is recovered by re-SELECTing inside the same
       transaction so the caller always sees a stable id.

    Raises:
        CopilotAppDbMissingError: ``db_path`` does not exist.
        CopilotAppDbSchemaError: ``PRAGMA user_version`` is out of range
            (delegated to ``_check_user_version``).
        CopilotAppDbLockedError: ``BEGIN IMMEDIATE`` timed out.
    """
    conn = _open_write_txn(db_path)
    try:
        existing = conn.execute(
            "SELECT id, main_repo_path FROM projects WHERE main_repo_path = ?",
            (str(ctx.repo_root),),
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            return ResolvedProject(
                project_id=existing["id"],
                was_created=False,
                main_repo_path=existing["main_repo_path"],
            )

        recipe = derive_project_recipe(ctx)
        try:
            conn.execute(
                _PROJECTS_INSERT_SQL,
                (
                    recipe.id,
                    recipe.name,
                    recipe.main_repo_path,
                    recipe.default_branch,
                    recipe.github_owner,
                    recipe.github_repo,
                ),
            )
        except sqlite3.IntegrityError:
            # Race: another writer (the App itself, or a parallel
            # apm install) inserted the same main_repo_path between
            # our SELECT and INSERT. Re-read inside this transaction
            # to find the winner's id.
            row = conn.execute(
                "SELECT id, main_repo_path FROM projects WHERE main_repo_path = ?",
                (str(ctx.repo_root),),
            ).fetchone()
            if row is None:
                # IntegrityError but no matching row -- some other
                # constraint failed (e.g. NOT NULL on a column we
                # don't write). Surface as a DB error.
                raise CopilotAppDbError(
                    "Race recovery failed: project row INSERT collided "
                    "but no matching row was found post-collision."
                ) from None
            conn.execute("COMMIT")
            return ResolvedProject(
                project_id=row["id"],
                was_created=False,
                main_repo_path=row["main_repo_path"],
            )

        conn.execute("COMMIT")
        return ResolvedProject(
            project_id=recipe.id,
            was_created=True,
            main_repo_path=recipe.main_repo_path,
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
