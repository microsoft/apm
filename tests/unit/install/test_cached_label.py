"""Unit tests for F2 (microsoft/apm#1116): cached label is suppressed
when the resolver callback fetched the package in the same run.

Bug repro: on a fresh install, the resolver callback downloads
``owner/repo``, then the integrate phase sees ``skip_download=True``
(``already_resolved`` is true), routes to ``CachedDependencySource``,
and previously emitted the install line with ``cached=True``. The
suffix told the user "(cached)" for bytes that were just downloaded.

Fix: ``CachedDependencySource`` now takes an explicit
``fetched_this_run`` and inverts it for the ``cached`` flag passed to
``logger.download_complete``. The ``make_dependency_source`` factory
plumbs the value through, and the integrate phase computes it from
``ctx.callback_downloaded``.
"""

from pathlib import Path
from unittest.mock import MagicMock

from apm_cli.install.sources import CachedDependencySource


def _make_source(*, fetched_this_run: bool, sha: str = "abcd1234deadbeef"):
    ctx = MagicMock()
    ctx.targets = []  # short-circuit acquire() before integration
    ctx.logger = MagicMock()

    dep_ref = MagicMock()
    dep_ref.is_virtual = False
    dep_ref.repo_url = "https://github.com/owner/repo"
    dep_ref.reference = "v1.2.3"

    dep_locked_chk = MagicMock()
    dep_locked_chk.resolved_commit = sha

    return CachedDependencySource(
        ctx=ctx,
        dep_ref=dep_ref,
        install_path=Path("/tmp/fake-install-path"),
        dep_key="owner/repo@v1.2.3",
        resolved_ref=None,
        dep_locked_chk=dep_locked_chk,
        fetched_this_run=fetched_this_run,
    )


def test_cached_source_default_passes_cached_true():
    src = _make_source(fetched_this_run=False)
    src.acquire()
    kwargs = src.ctx.logger.download_complete.call_args.kwargs
    assert kwargs["cached"] is True


def test_cached_source_fetched_this_run_passes_cached_false():
    """When the resolver callback downloaded this package earlier in
    the same install, the ``cached`` flag must flip to False so the
    user does not see a misleading "(cached)" suffix."""
    src = _make_source(fetched_this_run=True)
    src.acquire()
    kwargs = src.ctx.logger.download_complete.call_args.kwargs
    assert kwargs["cached"] is False


def test_make_dependency_source_plumbs_fetched_flag():
    """The factory must forward ``fetched_this_run`` so the integrate
    phase can drive the label end-to-end."""
    from apm_cli.install.sources import make_dependency_source

    ctx = MagicMock()
    dep_ref = MagicMock()
    dep_ref.is_local = False
    dep_ref.local_path = None
    dep_locked_chk = MagicMock()
    dep_locked_chk.resolved_commit = "abcd1234deadbeef"

    src = make_dependency_source(
        ctx,
        dep_ref,
        Path("/tmp/x"),
        "owner/repo@v1",
        resolved_ref=None,
        dep_locked_chk=dep_locked_chk,
        skip_download=True,
        fetched_this_run=True,
    )
    assert isinstance(src, CachedDependencySource)
    assert src.fetched_this_run is True
