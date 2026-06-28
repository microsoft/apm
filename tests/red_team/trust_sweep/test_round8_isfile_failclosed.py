"""Round-8 trust regression trap: project-tier is_file() fail-closed.

r8-trust-2 (LOW) -- ``build_runner_from_context`` probed the project
``apm.yml`` with ``Path.is_file()`` BEFORE the trust check. CPython's
``Path.is_file()`` only swallows ENOENT / ENOTDIR / EBADF / ELOOP via
``_ignore_error``; an unreadable parent directory (EACCES) or a
concurrent hostile swap (EINVAL / ENAMETOOLONG) therefore propagated an
uncaught ``OSError`` out of the firing boundary, aborting
``apm install`` / ``update`` / ``uninstall``. The fix wraps the
project-tier discovery region in ``try/except OSError`` that degrades to
an empty (untrusted) project tier.
"""

from __future__ import annotations

import os

import pytest

from apm_cli.core.lifecycle_scripts import build_runner_from_context

pytestmark = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses directory permission bits",
)


def test_unreadable_parent_dir_fails_closed(tmp_path, monkeypatch):
    """An EACCES on the project apm.yml probe must skip the tier, not abort."""
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    parent = tmp_path / "parent"
    parent.mkdir()
    child = parent / "proj"
    child.mkdir()
    (child / "apm.yml").write_text(
        "lifecycle:\n  post-install:\n    - {type: command, command: echo hi}\n"
    )
    original_mode = parent.stat().st_mode
    os.chmod(parent, 0o000)
    try:
        runner = build_runner_from_context(project_root=child)
    finally:
        os.chmod(parent, original_mode)

    # The build must succeed (no abort) with the project tier skipped.
    assert runner is not None
    assert [s for s in runner._scripts if s.source == "project"] == []
