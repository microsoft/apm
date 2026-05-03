"""Shared Live-region TUI controller for the install pipeline.

PR #1116 / workstream B.

A single ``InstallTui`` instance is opened by ``apm install`` and is
re-used across the resolve, download, integrate, and MCP-registry
phases.  Per-phase code calls ``start_phase()`` once when the phase
boundary is crossed, then ``task_started()`` / ``task_completed()`` /
``task_failed()`` for every dep / server / artifact in flight.

The Live region is **deferred** by 250 ms after open so that an
install that finishes from a warm cache or completes in <250 ms never
flashes a spinner.  The ``should_animate()`` predicate gates the whole
controller on TTY capabilities and the ``APM_PROGRESS`` env knob.

Notes for callers
-----------------

* Always wrap the lifecycle in ``with ctx.tui:``.  The context
  manager owns ``Live.stop()`` in the ``__exit__`` path and is the
  only safe place to tear the Live region down.
* When ``should_animate()`` is False (CI, dumb terminal,
  ``APM_PROGRESS=never``, ``--quiet``), every method on this class is
  a cheap no-op.  Callers do NOT need to gate their calls.
* This module deliberately uses a single ASCII spinner
  (``spinner_name="line"`` => ``| / - \\``) and never emits emoji or
  Unicode box-drawing, to stay safe under Windows cp1252.
"""

from __future__ import annotations

import contextlib
import os
import threading
from typing import Any

from apm_cli.utils.console import _get_console

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Defer the Live region for 250 ms after entering the context manager.
# Installs that finish under this threshold never paint a spinner.
_DEFER_SHOW_S: float = 0.250

# Rich refresh rate for the Live region.  8 Hz keeps the spinner alive
# without cursor flicker on conhost / SSH.  See proposal section 16.
_REFRESH_HZ: int = 8

# Maximum number of in-flight task labels to show before collapsing the
# tail to "... and N more".  Two-line bound on vertical real estate.
_MAX_VISIBLE_LABELS: int = 4


# ---------------------------------------------------------------------------
# TTY / env detection
# ---------------------------------------------------------------------------


def should_animate() -> bool:
    """Return True iff the install pipeline should paint a Live region.

    Resolution order (first match wins):

    1. ``APM_PROGRESS=never`` or ``=quiet`` -- never animate.
    2. ``APM_PROGRESS=always`` -- always animate (intended for local
       debugging; CI MUST NOT set this).
    3. ``APM_PROGRESS=auto`` (default) -- animate iff the console is
       an interactive TTY AND ``TERM`` is not ``""`` / ``"dumb"`` AND
       ``CI`` is not truthy.

    The function intentionally does NOT consult ``--quiet`` itself;
    the CLI front-end is responsible for setting ``APM_PROGRESS=quiet``
    (or never instantiating ``InstallTui``) in that case.
    """
    mode = os.environ.get("APM_PROGRESS", "auto").strip().lower()
    if mode in ("never", "quiet", "off", "0", "false", "no"):
        return False
    if mode in ("always", "on", "1", "true", "yes"):
        return True
    # mode == "auto" (or unrecognized -- treat as auto)
    if os.environ.get("CI", "").strip().lower() in ("1", "true", "yes"):
        return False
    if os.environ.get("TERM", "").strip().lower() in ("", "dumb"):
        return False
    c = _get_console()
    if c is None:
        return False
    try:
        return bool(getattr(c, "is_terminal", False)) and bool(getattr(c, "is_interactive", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class InstallTui:
    """One Live region for the entire install lifecycle.

    Public API (all calls are no-ops when the controller is disabled):

    * ``__enter__`` / ``__exit__`` -- context-manager protocol.
    * ``start_phase(name, total)`` -- swap the aggregate progress bar
      to a fresh task for the named phase.
    * ``task_started(key, label)`` -- add ``label`` to the in-flight
      label set.  Idempotent on label.
    * ``task_completed(key, milestone)`` -- remove labels matching
      ``key`` from the in-flight set, advance the phase bar, and
      optionally emit ``milestone`` as a non-transient line above the
      Live region.
    * ``task_failed(key, milestone)`` -- alias for ``task_completed``;
      callers are expected to format ``milestone`` with ``[x]``.
    * ``is_animating()`` -- True iff the Live region is currently
      visible (i.e. the defer threshold elapsed and the controller is
      enabled).  Used by the resolving heartbeat to suppress its
      static line.
    """

    def __init__(self) -> None:
        self.console = _get_console()
        self._enabled: bool = should_animate()

        # Lazily build the Rich primitives so non-animating installs
        # do not import or instantiate Progress / Live at all.
        self._aggregate: Any | None = None
        self._task_id: Any | None = None
        self._labels: list[str] = []
        # Per-key tracking so task_completed(key) can drop the right
        # label even when callers use a human-readable label that does
        # not embed the dep key.  Insertion-ordered for stable display.
        self._key_to_label: dict[str, str] = {}
        self._lock = threading.Lock()
        self._live: Any | None = None
        self._timer: threading.Timer | None = None
        # Sentinel to close a TOCTOU race between __exit__ on the main
        # thread and the deferred-start callback on the Timer thread:
        # if the timer is past cancel() but has not yet assigned _live,
        # _defer_start checks _shutdown after constructing Live and
        # before .start() so the region is never left running unowned.
        self._shutdown: bool = False

    # -- Context-manager lifecycle ----------------------------------------
    #
    # NOTE: This controller supports MULTIPLE enter/exit cycles on the
    # same instance. ``__exit__`` only tears down the Live region and
    # the deferred-show timer; ``_aggregate``, ``_labels``, and
    # ``_key_to_label`` survive so a follow-on ``__enter__`` can resume
    # rendering. The install pipeline relies on this: it wraps resolve
    # and the post-resolve body in two separate ``with`` blocks so an
    # early-exit "nothing to do" path can cleanly tear the Live region
    # down without losing phase state.

    def __enter__(self) -> InstallTui:
        if self._enabled:
            with self._lock:
                self._shutdown = False
            self._timer = threading.Timer(_DEFER_SHOW_S, self._defer_start)
            self._timer.daemon = True
            self._timer.start()
        return self

    def __exit__(self, *exc: Any) -> bool:
        # Set shutdown sentinel BEFORE cancel() so the Timer thread can
        # observe it and bail out even if it raced past the cancel.
        with self._lock:
            self._shutdown = True
        # ALWAYS cancel the deferred-start timer first; if cancel()
        # returns True the timer has not fired and we never built a
        # Live, so there is nothing to stop.
        if self._timer is not None:
            with contextlib.suppress(Exception):
                self._timer.cancel()
            self._timer = None
        if self._live is not None:
            # Rich teardown is best-effort; never let Live cleanup
            # mask a real install error propagating from the body.
            with contextlib.suppress(Exception):
                self._live.stop()
            self._live = None
        return False  # do not suppress exceptions

    # -- Internal: build & start the Live region --------------------------

    def _build_aggregate(self) -> Any:
        """Lazily construct the Rich ``Progress`` primitive.

        Uses a custom ASCII bar column instead of Rich's default ``BarColumn``
        because the latter renders Unicode block-drawing glyphs (U+2501 etc)
        that violate the cp1252 ASCII-only output contract.
        """
        from rich.progress import (
            Progress,
            ProgressColumn,
            SpinnerColumn,
            TaskProgressColumn,
            TextColumn,
            TimeElapsedColumn,
        )
        from rich.text import Text

        class _AsciiBarColumn(ProgressColumn):
            """ASCII-only progress bar: ``[####........]``."""

            def __init__(self, bar_width: int = 28) -> None:
                super().__init__()
                self._bar_width = bar_width

            def render(self, task: Any) -> Any:
                pct = task.percentage if task.total else 0.0
                filled = round(self._bar_width * (pct / 100.0))
                filled = max(0, min(self._bar_width, filled))
                bar = "#" * filled + "." * (self._bar_width - filled)
                return Text(f"[{bar}]")

        return Progress(
            _AsciiBarColumn(bar_width=28),
            TaskProgressColumn(),
            TextColumn("{task.fields[phase]}"),
            SpinnerColumn(spinner_name="line"),  # ASCII: | / - \
            TimeElapsedColumn(),
            console=self.console,
            refresh_per_second=_REFRESH_HZ,
            transient=True,
        )

    def _defer_start(self) -> None:
        """Timer callback: open the Live region after the defer window."""
        try:
            with self._lock:
                if self._shutdown or self._live is not None:
                    return
            from rich.console import Group
            from rich.live import Live

            if self._aggregate is None:
                self._aggregate = self._build_aggregate()
            live = Live(
                Group(self._aggregate, self._labels_renderable()),
                console=self.console,
                refresh_per_second=_REFRESH_HZ,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            # Re-check shutdown sentinel under the lock just before
            # publishing the Live reference and starting it. If __exit__
            # set _shutdown after our first check (race window), bail
            # out before .start() so the region is never left orphaned.
            with self._lock:
                if self._shutdown:
                    return
                self._live = live
            self._live.start(refresh=True)
        except Exception:
            # Defensive: a Live failure must NEVER take the install
            # down with it.  Disable the controller and continue.
            self._enabled = False
            self._live = None

    def _labels_renderable(self) -> Any:
        """Render the in-flight label list (called under the live refresh)."""
        from rich.text import Text

        with self._lock:
            if not self._labels:
                return Text("")
            visible = self._labels[:_MAX_VISIBLE_LABELS]
            head = "  > " + "  ".join(visible)
            extra = len(self._labels) - len(visible)
            if extra > 0:
                head += f"  ... and {extra} more"
            return Text(head, style="cyan")

    def _refresh_group(self) -> None:
        """Re-render the Live group (aggregate bar + labels)."""
        if self._live is None:
            return
        try:
            from rich.console import Group

            self._live.update(
                Group(self._aggregate, self._labels_renderable()),
                refresh=False,
            )
        except Exception:
            pass

    # -- Public API -------------------------------------------------------

    def is_animating(self) -> bool:
        """True iff the Live region is currently painted."""
        return self._enabled and self._live is not None

    def start_phase(self, name: str, total: int | None) -> None:
        """Swap the aggregate bar to a fresh task for ``name``.

        ``total`` is the count of task units in this phase (deps to
        download, integrators to run, etc.).  ``None`` is treated as
        ``1`` so the bar is well-formed but never completes from
        ``advance()`` calls alone.
        """
        if not self._enabled:
            return
        if self._aggregate is None:
            self._aggregate = self._build_aggregate()
        if self._task_id is not None:
            with contextlib.suppress(Exception):
                self._aggregate.remove_task(self._task_id)
            self._task_id = None
        # Clear stale labels from prior phase so the active-set list does
        # not bleed across phase boundaries.
        with self._lock:
            self._key_to_label.clear()
            self._labels = []
        self._task_id = self._aggregate.add_task(
            "", total=(total if total and total > 0 else 1), phase=name
        )
        self._refresh_group()

    def task_started(self, key: str, label: str) -> None:
        """Add ``label`` to the in-flight label list (de-duped on key)."""
        if not self._enabled:
            return
        with self._lock:
            if key not in self._key_to_label:
                self._key_to_label[key] = label
                if label not in self._labels:
                    self._labels.append(label)
        self._refresh_group()

    def task_completed(self, key: str, milestone: str | None = None) -> None:
        """Drop the label registered for ``key``, advance the phase bar.

        If ``milestone`` is provided, it is printed above the Live
        region as a permanent line (the Live region is transient and
        will be torn down at exit).
        """
        if not self._enabled:
            return
        with self._lock:
            label = self._key_to_label.pop(key, None)
            if label is not None:
                # A label may legitimately be shared by two keys; only
                # drop it from the visible list when no other key is
                # still using it.
                if label not in self._key_to_label.values():
                    self._labels = [lbl for lbl in self._labels if lbl != label]
        if self._aggregate is not None and self._task_id is not None:
            with contextlib.suppress(Exception):
                self._aggregate.advance(self._task_id, 1)
        if milestone and self._live is not None:
            # Rich's Console acquires its own internal lock here; do
            # NOT wrap with self._lock (would deadlock with the
            # refresh thread).
            with contextlib.suppress(Exception):
                self._live.console.print(milestone)
        self._refresh_group()

    def task_failed(self, key: str, milestone: str | None = None) -> None:
        """Same lifecycle as :meth:`task_completed`; caller marks failure."""
        self.task_completed(key, milestone)
