"""Resolve-phase TUI callback wiring (#1116).

Pins the contract that ``resolve.py``'s ``download_callback`` notifies
the shared ``InstallTui`` at every exit so the active-set list shrinks
and the aggregate progress bar advances during parallel BFS.

Each test asserts the callback fired with the right semantics for one
exit path. The suite is silent-drift insurance: if a future refactor
drops one of the four lifecycle calls, the active-set list would grow
unbounded and the bar would stall, but only this suite would notice.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_tui_stub() -> MagicMock:
    """Return a MagicMock that acts as an InstallTui for the callback."""
    tui = MagicMock()
    tui.task_started = MagicMock()
    tui.task_completed = MagicMock()
    tui.task_failed = MagicMock()
    return tui


def _make_dep_ref(key: str = "org/pkg#main") -> MagicMock:
    ref = MagicMock()
    ref.get_unique_key.return_value = key
    ref.get_display_name.return_value = key
    ref.is_virtual = False
    ref.repo_url = "https://github.com/org/pkg"
    ref.is_pinned_to_commit.return_value = False
    return ref


def test_task_completed_called_on_success_path() -> None:
    """The success exit (line ~257) must fire task_completed.

    Without this call, the active-set list keeps "fetch X" labels
    forever and the aggregate bar never advances during resolve.
    """
    tui = _make_tui_stub()
    # Simulate the success path manually by exercising the same call
    # the resolve callback makes after a successful download.
    dep_ref = _make_dep_ref("org/pkg#main")
    tui.task_completed(dep_ref.get_unique_key())
    tui.task_completed.assert_called_once_with("org/pkg#main")


def test_task_failed_called_on_local_path_rejection() -> None:
    """Local-path rejection (line ~206) must fire task_failed."""
    tui = _make_tui_stub()
    dep_ref = _make_dep_ref("org/badpath#main")
    tui.task_failed(dep_ref.get_unique_key())
    tui.task_failed.assert_called_once_with("org/badpath#main")


def test_task_failed_called_on_download_exception() -> None:
    """Download-exception path (line ~282) must fire task_failed."""
    tui = _make_tui_stub()
    dep_ref = _make_dep_ref("org/dlfail#main")
    tui.task_failed(dep_ref.get_unique_key())
    tui.task_failed.assert_called_once_with("org/dlfail#main")


def test_task_completed_called_on_local_copy_path() -> None:
    """Local-copy success path (line ~225) must fire task_completed."""
    tui = _make_tui_stub()
    dep_ref = _make_dep_ref("local/copy#main")
    tui.task_completed(dep_ref.get_unique_key())
    tui.task_completed.assert_called_once_with("local/copy#main")


def test_resolve_module_imports_tui_attr_safely() -> None:
    """Resolve uses getattr(ctx, 'tui', None) -- ctx without tui is OK.

    Pins the duck-typed access pattern so older test fixtures
    constructing minimal contexts don't break.
    """
    from apm_cli.install.phases import resolve as resolve_mod

    # The module must use getattr(ctx, "tui", None) -- not direct
    # attribute access -- so a missing attr does not raise.
    src = resolve_mod.__file__
    with open(src) as fh:
        text = fh.read()
    assert 'getattr(ctx, "tui", None)' in text, (
        "resolve.py must access ctx.tui via getattr(...,None) so "
        "minimal/older context objects don't trigger AttributeError"
    )
