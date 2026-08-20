"""Pure-logic helpers for the per-package skip / locked-ref decisions.

These functions are the single source of truth for two key boolean decisions
in the install pipeline.  Both the sequential integration loop
(``phases/integrate.py``) and the unit test suite import from here, so the
tests exercise the real condition rather than a mirrored copy.
"""

from __future__ import annotations


def _compute_skip_download(
    install_path_exists: bool,
    is_cacheable: bool,
    update_refs: bool,
    already_resolved: bool,
    lockfile_match: bool,
    ref_changed: bool = False,
) -> bool:
    """Return True when the sequential loop should skip downloading a package.

    A package can be skipped when the install path already exists on disk AND
    at least one of the following is true:

    * The ref is a pinned tag / commit (``is_cacheable``) and the user has not
      requested an update (``not update_refs``).
    * The BFS callback already fetched the package (``already_resolved``) and
      the user has not requested an update.
    * The lockfile SHA matches the local checkout (``lockfile_match``).

    ``ref_changed`` vetoes the two cache-reuse clauses.  When the manifest ref
    drifted away from the lockfile the user explicitly asked for a different
    revision, so bytes already sitting at ``install_path`` must never be
    trusted -- not even when the BFS callback fetched this dep earlier in the
    same run, because the callback materialises to the canonical
    ``<org>/<repo>`` path while an aliased dep integrates from
    ``apm_modules/<alias>`` (#2481).  ``lockfile_match`` is left untouched: it
    is only set in normal mode when the ref did not change, and in update mode
    after the remote SHA was confirmed identical.

    Callers may further override this result (e.g. content-hash verification
    or registry-only enforcement) -- this function only computes the initial
    decision.
    """
    return install_path_exists and (
        (is_cacheable and not update_refs and not ref_changed)
        or (already_resolved and not update_refs and not ref_changed)
        or lockfile_match
    )


def _should_use_locked_ref(locked_ref: str | None, update_refs: bool) -> bool:
    """Return True when the download should be pinned to the locked commit SHA.

    Uses the locked commit SHA from the lockfile for byte-for-byte
    reproducibility.  Returns False when:

    * ``locked_ref`` is absent or falsy -- no SHA was recorded.
    * ``locked_ref == "cached"`` -- sentinel meaning no real SHA is stored.
    * ``update_refs`` is True -- the user explicitly requested re-resolution.

    Note: in ``build_download_ref`` (drift.py) the caller already gates the
    entire locked-dep block on ``not update_refs`` (outer guard at L314), so
    the ``not update_refs`` check here is redundant at that call site.  It is
    kept intentionally so the helper is self-contained and correct when called
    from future contexts that lack the outer guard.
    """
    return bool(locked_ref) and locked_ref != "cached" and not update_refs
