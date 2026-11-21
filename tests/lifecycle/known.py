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


def signature_mismatch(item: pytest.Item, error: BaseException) -> str | None:
    """Why an expected failure of *item* does not count, or ``None`` when it does."""
    mark = item.get_closest_marker("xfail")
    match = mark.kwargs.get("match") if mark is not None else None
    if not match or re.search(match, str(error)):
        return None
    return (
        f"strict known cell {mark.kwargs.get('reason', '')!r} failed for a different "
        f"reason: its signature /{match}/ is not in the failure message. "
        "Fix the new cause or re-key the cell."
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """Report a strict known cell that failed without its signature as a failure."""
    report = yield
    if report.when == "call" and hasattr(report, "wasxfail") and call.excinfo is not None:
        problem = signature_mismatch(item, call.excinfo.value)
        if problem is not None:
            report.outcome = "failed"
            del report.wasxfail
            report.sections.append(("known cell signature", problem))
    return report
