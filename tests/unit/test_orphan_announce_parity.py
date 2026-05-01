"""Round-3 panel regressions: orphan-announce channel parity.

Both `apm prune` and `apm deps list` surface the same semantic event
("orphans found") and must therefore use the same logger channel:

  * Orphan-found header + per-package bullets -> ``logger.warning``
    (a destructive command must be at least as loud as an advisory
    display command, and bullets are subordinate context of that
    warning -- not transient progress narration).
  * Recovery hint ("Run 'apm prune'...") -> ``logger.info``
    (advisory remediation, not the problem statement; using
    ``logger.progress`` risks suppression in ``--quiet`` / CI mode and
    silently drops the actionable hint).

These tests pin the channel mapping so a future refactor cannot
silently regress to ``logger.progress``.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_APM_YML_NO_DEPS = "name: t\nversion: 1.0.0\ndependencies:\n  apm: []\n"


def _make_orphan_dir(tmp: Path, owner: str, repo: str) -> Path:
    pkg = tmp / "apm_modules" / owner / repo
    pkg.mkdir(parents=True)
    (pkg / "apm.yml").write_text("name: r\nversion: 1.0.0", encoding="utf-8")
    return pkg


@contextlib.contextmanager
def _chdir_tmp():
    with tempfile.TemporaryDirectory() as td:
        prev = Path.cwd()
        os.chdir(td)
        try:
            yield Path(td)
        finally:
            os.chdir(prev)


def _capture_logger_calls():
    """Return a (logger_factory, calls) tuple.

    The factory is suitable for patching CommandLogger; ``calls`` is a
    list of ``(method_name, message)`` tuples in invocation order.
    """
    calls: list[tuple[str, str]] = []

    def _make(*_args, **_kwargs):
        logger = MagicMock()

        def _record(name):
            def _inner(msg, *a, **k):
                calls.append((name, str(msg)))

            return _inner

        for method in (
            "info",
            "warning",
            "error",
            "success",
            "progress",
            "start",
            "debug",
        ):
            setattr(logger, method, _record(method))
        return logger

    return _make, calls


# ---------------------------------------------------------------------------
# Parity: orphan-found header + bullets routed via logger.warning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command, patch_target",
    [
        (["prune", "--dry-run"], "apm_cli.commands.prune.CommandLogger"),
        (["deps", "list"], "apm_cli.commands.deps.cli.CommandLogger"),
    ],
)
def test_orphan_announce_level_parity_prune_vs_deps_cli(command, patch_target):
    """Both surfaces emit the orphan-found header AND each per-package
    bullet through ``logger.warning`` -- never ``logger.progress``.
    """
    runner = CliRunner()
    factory, calls = _capture_logger_calls()
    with _chdir_tmp() as tmp:
        (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
        _make_orphan_dir(tmp, "orphan-org", "orphan-repo")
        with patch(patch_target, side_effect=factory):
            result = runner.invoke(cli, command)
        assert result.exit_code == 0, result.output

    warning_msgs = [m for (lvl, m) in calls if lvl == "warning"]
    progress_msgs = [m for (lvl, m) in calls if lvl == "progress"]

    # Header (substring match -- exact text differs slightly between commands)
    assert any("orphan" in m.lower() and "package" in m.lower() for m in warning_msgs), (
        f"Orphan-found header must be at warning level. "
        f"warnings={warning_msgs!r} progress={progress_msgs!r}"
    )
    # Per-package bullet for the orphan must be at warning level too.
    assert any("orphan-org/orphan-repo" in m for m in warning_msgs), (
        f"Per-package orphan bullet must be at warning level. "
        f"warnings={warning_msgs!r} progress={progress_msgs!r}"
    )
    # And must NOT appear at progress level (the regression we are
    # pinning closed).
    assert not any("orphan-org/orphan-repo" in m for m in progress_msgs), (
        f"Per-package orphan bullet must NOT be emitted at progress "
        f"level. progress={progress_msgs!r}"
    )


def test_orphan_recovery_hint_uses_info_not_progress():
    """The ``Run 'apm prune' to remove orphaned packages`` hint emitted
    by ``apm deps list`` is advisory remediation context. It must use
    ``logger.info`` so it survives quiet/CI suppression of the
    in-flight ``progress`` channel.
    """
    runner = CliRunner()
    factory, calls = _capture_logger_calls()
    with _chdir_tmp() as tmp:
        (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
        _make_orphan_dir(tmp, "orphan-org", "orphan-repo")
        with patch("apm_cli.commands.deps.cli.CommandLogger", side_effect=factory):
            result = runner.invoke(cli, ["deps", "list"])
        assert result.exit_code == 0, result.output

    info_msgs = [m for (lvl, m) in calls if lvl == "info"]
    progress_msgs = [m for (lvl, m) in calls if lvl == "progress"]

    assert any("apm prune" in m for m in info_msgs), (
        f"Recovery hint must be emitted at info level. "
        f"info={info_msgs!r} progress={progress_msgs!r}"
    )
    assert not any("apm prune" in m for m in progress_msgs), (
        f"Recovery hint must NOT be emitted at progress level. progress={progress_msgs!r}"
    )
