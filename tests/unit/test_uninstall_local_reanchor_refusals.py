"""A re-anchor refuses before any write when it cannot run as one transaction (#4889)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apm_cli.commands.uninstall.cli import _plan_reanchor_or_exit

pytestmark = [pytest.mark.component, pytest.mark.promise("P-INV-I3", "P-INV-I1")]


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("shrink", "select fewer skills or targets of local:vendor/f"),
        ("native", "native plugin registrations are reconciled separately"),
        ("mixed", "uninstall ./vendor/r in a separate command"),
    ],
)
def test_a_reanchor_that_cannot_be_one_transaction_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str, expected: str
) -> None:
    plan = SimpleNamespace(renames={"./vendor/e": "local:vendor/e"})
    monkeypatch.setattr(
        "apm_cli.commands.uninstall.local_reanchor.plan_local_reanchor",
        lambda *args, **kwargs: plan,
    )
    monkeypatch.setattr(
        "apm_cli.core.deployment_reanchor.reanchor_receipt_problems", lambda *args: []
    )
    monkeypatch.setattr(
        "apm_cli.claude_plugins.registrar.native_ownership",
        lambda lockfile: {"plugin": object()} if shape == "native" else {},
    )
    logger = MagicMock()

    with pytest.raises(SystemExit) as exc:
        _plan_reanchor_or_exit(
            MagicMock(),
            SimpleNamespace(),
            [],
            tmp_path / "apm.yml",
            logger,
            others=["./vendor/r"] if shape == "mixed" else [],
            shrinks=lambda: {"local:vendor/f": object()} if shape == "shrink" else {},
        )

    assert exc.value.code == 1
    message = " ".join(str(call.args[0]) for call in logger.error.call_args_list)
    assert expected in message, message
    assert "apm.yml and apm.lock.yaml were not changed" in message


def test_a_local_row_without_a_path_is_unverified_offline(tmp_path: Path) -> None:
    """An empty local_path names no target to check, so the removal refuses."""
    from apm_cli.commands.uninstall.cli import _refuse_unverifiable_local_survivors

    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        "name: proj\nversion: 1.0.0\ndependencies:\n  apm:\n    - ./vendor/e\n    - ./vendor/d\n",
        encoding="utf-8",
    )
    lockfile = MagicMock()
    lockfile.get_dependency.return_value = SimpleNamespace(source="local", local_path="")
    lockfile.replacements = None
    logger = MagicMock()

    with pytest.raises(SystemExit) as exc:
        _refuse_unverifiable_local_survivors(lockfile, ["./vendor/e"], manifest, logger)

    assert exc.value.code == 1
    message = " ".join(str(call.args[0]) for call in logger.error.call_args_list)
    assert "apm_modules/ is missing" in message, message
