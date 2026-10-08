"""Whether retained project rows give ``apm install`` work (#3179)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.install.helpers.no_work import local_rows_need_reconciling, retained_local_work

pytestmark = pytest.mark.unit


def _lockfile() -> LockFile:
    lockfile = LockFile()
    lockfile.local_deployed_files = [".agents/skills/demo/SKILL.md"]
    lockfile.local_deployed_file_hashes = {".agents/skills/demo/SKILL.md": "sha256:demo"}
    return lockfile


@pytest.mark.parametrize(
    ("explicit", "manifest", "configured", "markers", "expected"),
    [
        ("claude", None, None, (), True),
        (None, ["claude"], None, (), True),
        (None, None, "claude", (), True),
        (None, None, None, (".claude",), True),
        (None, None, None, (), False),
        (None, None, None, (".claude", ".cursor"), False),
    ],
    ids=[
        "target-flag",
        "manifest-targets",
        "config-default",
        "detected-harness",
        "no-target",
        "ambiguous-harnesses",
    ],
)
def test_retained_rows_need_a_resolvable_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: str | None,
    manifest: list[str] | None,
    configured: str | None,
    markers: tuple[str, ...],
    expected: bool,
) -> None:
    """The decision follows the canonical target precedence and never raises."""
    monkeypatch.setattr("apm_cli.config.get_install_target", lambda **_: configured)
    monkeypatch.setattr(
        "apm_cli.models.apm_package.package_target_selection", lambda _package: manifest
    )
    for marker in markers:
        (tmp_path / marker).mkdir()

    assert (
        local_rows_need_reconciling(
            _lockfile(), tmp_path, SimpleNamespace(), target=explicit, scope=None
        )
        is expected
    )


def test_bundle_only_rows_are_not_install_work(tmp_path: Path) -> None:
    """Imperative bundle output is not reconciled by an ordinary install."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    lockfile = LockFile()
    bundled = ".agents/skills/bundled/SKILL.md"
    DeploymentLedgerCodec.record_local_bundle_files(
        lockfile, [bundled], {bundled: f"sha256:{'b' * 64}"}
    )

    assert not local_rows_need_reconciling(
        lockfile, tmp_path, SimpleNamespace(), target="claude", scope=None
    )


@pytest.mark.parametrize(
    ("value", "lockfile_only", "expected"),
    [(["claude"], False, True), (["claude"], True, False), (None, False, False)],
    ids=["resolved-target", "apm-lock", "unrestricted"],
)
def test_pipeline_reuses_its_target_decision(
    value: list[str] | None, lockfile_only: bool, expected: bool
) -> None:
    """The pipeline gate keys on the run's decision; ``apm lock`` returns early."""
    ctx = SimpleNamespace(target_decision=SimpleNamespace(value=value))

    assert retained_local_work(ctx, _lockfile(), lockfile_only) is expected
