"""Strict known failures: tracked and signed (#4565).

A bare strict xfail proves only that a row fails. After its fix lands, a row
can keep failing for a different reason and the xfail hides the new cause:
21 cells did so on 2026-11-21. A ``KnownFailure`` names its tracking issue
and its failure signature. This module is a pytest plugin (``tests/conftest.py``
registers it): it reports an expected failure whose message lacks the
signature as a failure. ``tests/quality/test_known_cells_cite_open_issues.py``
bans raw strict xfail marks under ``tests/lifecycle`` and keeps every tracking
issue open.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

TRACKING = re.compile(r"#[1-9]\d*")
# bug: the CLI is wrong today. deferred-design: the behavior waits on a design
# the maintainers deferred; its tracker stays open (#4543).
KINDS = frozenset({"bug", "deferred-design"})


@dataclass(frozen=True)
class KnownFailure:
    """A test that fails on main for one known, tracked reason."""

    tracking: str  # "#<open issue>"
    reason: str  # neutral phrase: what the CLI does wrong today
    match: str  # regex the failure message must contain
    kind: str = "bug"
    raises: type[BaseException] | tuple[type[BaseException], ...] = AssertionError

    def __post_init__(self) -> None:
        assert TRACKING.fullmatch(self.tracking), self.tracking
        assert self.reason and self.match, self
        assert self.kind in KINDS, self.kind
        re.compile(self.match)

    @property
    def label(self) -> str:
        return f"{self.tracking} {self.reason}"

    def mark(self) -> pytest.MarkDecorator:
        return pytest.mark.xfail(
            strict=True, raises=self.raises, reason=self.label, match=self.match, kind=self.kind
        )


# A lifecycle oracle failure reads "<header>:" then one line per violation, then
# the command evidence from "args=..." on (oracles.check_obligation, _settle).
_EVIDENCE = re.compile(r"^args=")


def _violations(message: str) -> list[str] | None:
    """The violation lines of a lifecycle oracle failure, or ``None`` for any other message."""
    lines = message.splitlines()
    end = next((i for i, line in enumerate(lines) if _EVIDENCE.match(line)), None)
    if end is None or end < 2 or not lines[0].endswith(":"):
        return None
    return [line for line in lines[1:end] if line.strip()]


def signature_mismatch(item: pytest.Item, error: BaseException) -> str | None:
    """Why an expected failure of *item* does not count, or ``None`` when it does.

    A lifecycle oracle failure counts only when EVERY violation line carries the
    signature, so an unrelated violation in the same assertion stays fatal. Any
    other failure counts when its message carries the signature.
    """
    mark = item.get_closest_marker("xfail")
    match = mark.kwargs.get("match") if mark is not None else None
    if not match:
        return None
    message = str(error)
    violations = _violations(message)
    if violations is None:
        stray = [] if re.search(match, message) else [message.splitlines()[0] if message else ""]
    else:
        stray = [line for line in violations if not re.search(match, line)]
    if not stray:
        return None
    return (
        f"strict known cell {mark.kwargs.get('reason', '')!r} failed for a different "
        f"reason: these lines lack its signature /{match}/: {stray}. "
        "Fix the new cause or re-key the cell."
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """Report a strict known cell that failed without its signature as a failure."""
    report = yield
    # pytest xfails a setup failure too, so every phase that raised is checked.
    if hasattr(report, "wasxfail") and call.excinfo is not None:
        problem = signature_mismatch(item, call.excinfo.value)
        if problem is not None:
            report.outcome = "failed"
            del report.wasxfail
            report.sections.append(("known cell signature", problem))
    return report
