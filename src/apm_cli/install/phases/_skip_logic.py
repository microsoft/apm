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
) -> bool:
    """Return True when the sequential loop should skip downloading a package.

    A package can be skipped when the install path already exists on disk AND
    at least one of the following is true:

    * The ref is a pinned tag / commit (``is_cacheable``) and the user has not
      requested an update (``not update_refs``).
    * The BFS callback already fetched the package (``already_resolved``) and
      the user has not requested an update.
    * The lockfile SHA matches the local checkout (``lockfile_match``).

    Callers may further override this result (e.g. content-hash verification
    or registry-only enforcement) -- this function only computes the initial
    decision.
    """
    return install_path_exists and (
        (is_cacheable and not update_refs)
        or (already_resolved and not update_refs)
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
