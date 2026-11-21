"""A local bundle whose embedded lockfile cannot be read is refused, not judged empty."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.bundle_admission import bundle_policy_subjects

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-policy-violation-subjects")]


def _bundle(root: Path, lock: str | None) -> SimpleNamespace:
    root.mkdir()
    if lock is not None:
        (root / "apm.lock.yaml").write_text(lock, encoding="utf-8")
    return SimpleNamespace(
        source_dir=root, package_id="tool-id", plugin_json={"name": "tool"}, lockfile=None
    )


@pytest.mark.parametrize(
    "lock",
    ["dependencies: [unclosed\n", "lockfile_version: '1'\ndependencies: not-a-list\n"],
)
def test_unreadable_bundle_lockfile_refuses_the_bundle(tmp_path: Path, lock: str) -> None:
    bundle = _bundle(tmp_path / "b", lock)

    with pytest.raises(ValueError, match=r"apm\.lock\.yaml"):
        bundle_policy_subjects(bundle)
