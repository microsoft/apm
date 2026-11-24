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
        ("lsp", "it supplies a language server"),
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
    lockfile = MagicMock()
    lockfile.lsp_config_provenance = {"pyright": "package:./vendor/e"} if shape == "lsp" else {}

    with pytest.raises(SystemExit) as exc:
        _plan_reanchor_or_exit(
            lockfile,
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


def test_every_declaration_of_one_directory_is_previewed_as_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two spellings of one directory are both kept in the preview, as the real run refuses."""
    from apm_cli.commands.uninstall.cli import _refuse_unverifiable_local_survivors
    from apm_cli.core.command_logger import CommandLogger

    (tmp_path / "vendor" / "e").mkdir(parents=True)
    survivor = tmp_path / "vendor" / "d"
    survivor.mkdir()
    (survivor / "apm.yml").write_text(
        "name: d\nversion: 1.0.0\ndependencies:\n  apm:\n    - ../e\n", encoding="utf-8"
    )
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        "name: proj\nversion: 1.0.0\ndependencies:\n  apm:\n"
        "    - ./vendor/e\n    - ./vendor/./e\n    - ./vendor/d\n",
        encoding="utf-8",
    )
    lockfile = MagicMock()
    lockfile.get_dependency.side_effect = [
        SimpleNamespace(source="local", local_path="./vendor/e"),
        SimpleNamespace(source="local", local_path="./vendor/./e"),
    ]
    lockfile.replacements = None

    kept = _refuse_unverifiable_local_survivors(
        lockfile,
        ["./vendor/e", "./vendor/./e"],
        manifest,
        CommandLogger("uninstall", dry_run=True),
        dry_run=True,
    )

    captured = capsys.readouterr()
    output = " ".join((captured.out + captured.err).split())
    assert kept == ["./vendor/e", "./vendor/./e"]
    assert "Would keep ./vendor/e:" in output and "Would keep ./vendor/./e:" in output, output


def test_the_survivor_read_drives_the_offline_reach_check(tmp_path: Path) -> None:
    """The survivors read from apm.yml decide the refusal: a reaches e, so e is kept."""
    from apm_cli.commands.uninstall.cli import _refuse_unverifiable_local_survivors

    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        "name: proj\nversion: 1.0.0\ndependencies:\n  apm:\n    - ./vendor/e\n    - ./vendor/a\n",
        encoding="utf-8",
    )
    for name, deps in (("e", ""), ("a", "dependencies:\n  apm:\n    - ../e\n")):
        (tmp_path / "vendor" / name).mkdir(parents=True)
        (tmp_path / "vendor" / name / "apm.yml").write_text(
            f"name: {name}\nversion: 1.0.0\n{deps}", encoding="utf-8"
        )
    lockfile = MagicMock()
    lockfile.get_dependency.return_value = SimpleNamespace(source="local", local_path="./vendor/e")
    lockfile.replacements = None
    logger = MagicMock()

    with pytest.raises(SystemExit) as exc:
        _refuse_unverifiable_local_survivors(lockfile, ["./vendor/e"], manifest, logger)

    assert exc.value.code == 1
    message = " ".join(str(call.args[0]) for call in logger.error.call_args_list)
    assert "apm_modules/ is missing" in message, message


@pytest.mark.parametrize(
    ("text", "declared"),
    [("- a\n- b\n", True), ("just a string\n", True), ("name: proj\n", False)],
    ids=["list", "scalar", "mapping"],
)
def test_a_non_mapping_manifest_counts_as_declaring_replacements(
    tmp_path: Path, text: str, declared: bool
) -> None:
    """An apm.yml that is not a mapping refuses the offline re-anchor like an
    unreadable one; a mapping without a replace block behaves as today (#5226)."""
    from apm_cli.commands.uninstall.local_reanchor import _manifest_declares_replacements

    manifest = tmp_path / "apm.yml"
    manifest.write_text(text, encoding="utf-8")

    assert _manifest_declares_replacements(manifest) is declared
    assert manifest.read_text(encoding="utf-8") == text
