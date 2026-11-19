"""``apm unpack`` writes bundle files only into managed integration directories.

Bundle lockfile ``deployed_files`` entries outside the known integration
prefixes are refused before any file is written, and bundle paths that run
through a symlink are never read.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from tests.integration._containment_kit import build_containment_kit, output, snapshot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_LOCK = "lockfile_version: '1'\ndependencies:\n- repo_url: o/r\n  deployed_files:\n{files}"


def _lock(*files: str) -> str:
    return _LOCK.format(files="".join(f"  - {f}\n" for f in files))


def test_unpack_refuses_unmanaged_destination(tmp_path: Path, apm_binary_path: Path) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    archive = kit.outside.parent / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("bundle/apm.lock.yaml", _lock(".github/prompts/p.prompt.md", ".git/config.d"))
        zf.writestr("bundle/.github/prompts/p.prompt.md", "Prompt body.\n")
        zf.writestr("bundle/.git/config.d", "[core]\n")
    (kit.project / ".git").mkdir()
    before = snapshot(kit.project)

    result = kit.run("unpack", str(archive))

    assert result.returncode != 0, output(result)
    assert "outside managed integration directories" in output(result)
    assert snapshot(kit.project) == before


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_unpack_does_not_read_through_linked_bundle_directory(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    (kit.outside / "notes.md").write_text("outside content\n", encoding="utf-8")
    bundle = kit.outside.parent / "bundle-dir"
    bundle.mkdir()
    (bundle / "apm.lock.yaml").write_text(_lock(".github/notes.md"), encoding="utf-8")
    os.symlink(kit.outside, bundle / ".github", target_is_directory=True)

    result = kit.run("unpack", str(bundle))

    assert result.returncode == 0, output(result)
    assert not (kit.project / ".github/notes.md").exists()
