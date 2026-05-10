"""Unit tests for ``install_root_redirect`` / ``compile_root_redirect``.

The redirect mutates two pieces of process-global state:

* the working directory (``os.chdir(root)``)
* the source-root override on :mod:`apm_cli.core.scope`

Both must be restored on every exit path -- success, exception, early
return -- so concurrent CLI invocations and embedded test runners do
not leak state into one another.

These tests are deliberately small and self-contained; they exercise
the context manager directly (no Click harness) so a regression that
breaks the unwind shows up here before any downstream test starts
seeing flaky cwd / pytest-tmpdir interactions.
"""

from __future__ import annotations

import os
from pathlib import Path

import click
import pytest

from apm_cli.core.scope import get_source_root_override
from apm_cli.install.root_redirect import (
    compile_root_redirect,
    install_root_redirect,
)


@pytest.fixture(autouse=True)
def _restore_cwd():
    """Snapshot + restore CWD around each test.

    Failures in the redirect logic could leave us inside ``tmp_path``
    after the test exits; that breaks every subsequent test that
    depends on the repo root.  The snapshot here is belt-and-braces
    insurance -- if a regression slips past the assertion that the
    redirect restored CWD, this fixture catches it before the next
    test gets confused.
    """
    original = Path.cwd()
    yield
    if Path.cwd() != original:
        os.chdir(original)


def test_noop_when_root_is_none(tmp_path):
    """``root=None`` is a no-op: cwd unchanged, no override set."""
    cwd_before = Path.cwd()
    assert get_source_root_override() is None

    with install_root_redirect(None):
        assert Path.cwd() == cwd_before
        assert get_source_root_override() is None

    assert Path.cwd() == cwd_before
    assert get_source_root_override() is None


def test_noop_when_root_is_empty_string():
    """Empty string is treated like ``None`` so callers can pass falsy."""
    cwd_before = Path.cwd()

    with install_root_redirect(""):
        assert Path.cwd() == cwd_before
        assert get_source_root_override() is None

    assert get_source_root_override() is None


def test_chdir_and_override_on_entry(tmp_path):
    """When *root* is set, cwd moves into it and override pins original."""
    cwd_before = Path.cwd()

    with install_root_redirect(tmp_path):
        assert Path.cwd().resolve() == tmp_path.resolve()
        assert get_source_root_override() == cwd_before.resolve()

    assert Path.cwd() == cwd_before
    assert get_source_root_override() is None


def test_restore_on_exception(tmp_path):
    """An exception inside the block must still restore cwd + override."""
    cwd_before = Path.cwd()

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with install_root_redirect(tmp_path):
            assert Path.cwd().resolve() == tmp_path.resolve()
            assert get_source_root_override() == cwd_before.resolve()
            raise _Boom("simulated install failure")

    assert Path.cwd() == cwd_before
    assert get_source_root_override() is None


def test_creates_target_dir_when_missing(tmp_path):
    """Normal mode creates *root* if absent (mirrors pip --target UX)."""
    deploy = tmp_path / "fresh"
    assert not deploy.exists()

    with install_root_redirect(deploy):
        assert deploy.is_dir()

    assert deploy.is_dir()


def test_dry_run_refuses_to_create_target(tmp_path):
    """``dry_run=True`` must NOT create *root* -- raises UsageError."""
    deploy = tmp_path / "missing"
    assert not deploy.exists()

    with pytest.raises(click.UsageError, match="does not exist"):
        with install_root_redirect(deploy, dry_run=True):
            pytest.fail("body should not execute")

    assert not deploy.exists(), "dry-run leaked a mkdir on disk"


def test_dry_run_works_when_target_exists(tmp_path):
    """When *root* already exists, ``dry_run=True`` enters cleanly."""
    cwd_before = Path.cwd()

    with install_root_redirect(tmp_path, dry_run=True):
        assert Path.cwd().resolve() == tmp_path.resolve()
        assert get_source_root_override() == cwd_before.resolve()

    assert Path.cwd() == cwd_before
    assert get_source_root_override() is None


def test_compile_alias_is_install_redirect():
    """The compile alias must be the same callable, not a copy.

    A copy would let one drift from the other on future edits; the
    intentional aliasing prevents that silent divergence.
    """
    assert compile_root_redirect is install_root_redirect
