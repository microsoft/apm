"""End-to-end regression: silent adoption of identical existing deployments.

This reproduces the degraded-lockfile catch-22 reported by zava-storefront:
when deployed files remain byte-identical but their lockfile provenance is
lost, reinstall must adopt them and restore ``deployed_files``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.utils.hermetic_packaged_sample import (
    HermeticPackagedSample,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]


def _read_lockfile(project_dir: Path) -> dict[str, object] | None:
    lock_path = project_dir / "apm.lock.yaml"
    if not lock_path.exists():
        return None
    with lock_path.open(encoding="utf-8") as handle:
        lockfile = yaml.safe_load(handle)
    assert lockfile is None or isinstance(lockfile, dict)
    return lockfile


def _all_deployed_files(lockfile: dict[str, object] | None) -> list[str]:
    """Flatten every deployed_files entry across all locked dependencies."""
    out: list[str] = []
    deps = (lockfile or {}).get("dependencies", [])
    if isinstance(deps, list):
        for entry in deps:
            out.extend(entry.get("deployed_files", []) or [])
    return out


class TestSilentAdoptOfExistingFiles:
    """Catch-22: degraded lockfile plus identical files on disk must self-heal."""

    def test_reinstall_with_wiped_lockfile_repopulates_deployed_files(
        self,
        hermetic_packaged_sample: HermeticPackagedSample,
    ) -> None:
        """The exact zava-storefront reproducer preserves deployed provenance."""
        project_dir = hermetic_packaged_sample.project.root
        result1 = hermetic_packaged_sample.run(
            ("install",),
            scenario_id="silent-adopt-initial-install",
        )
        assert result1.returncode == 0, (
            f"First install failed:\nstderr={result1.stderr}\nstdout={result1.stdout}"
        )

        lock1 = _read_lockfile(project_dir)
        files_before = sorted(_all_deployed_files(lock1))
        assert files_before, "Test precondition: first install must populate deployed_files"

        # deployed_files entries can be files or skill directories; only compare file bytes.
        disk_before = {
            path: (project_dir / path).read_bytes()
            for path in files_before
            if (project_dir / path).is_file()
        }
        assert disk_before, "Test precondition: at least one deployed file on disk"

        (project_dir / "apm.lock.yaml").unlink()

        result2 = hermetic_packaged_sample.run(
            ("install",),
            scenario_id="silent-adopt-reinstall",
        )
        assert result2.returncode == 0, (
            f"Re-install failed:\nstderr={result2.stderr}\nstdout={result2.stdout}"
        )

        lock2 = _read_lockfile(project_dir)
        files_after = sorted(_all_deployed_files(lock2))
        assert files_after == files_before, (
            "deployed_files lost after lockfile-wipe plus re-install. "
            "This is the catch-22: degraded lockfile cannot self-heal because "
            "non-skill integrators skip byte-identical files instead of adopting them.\n"
            f"  Before: {files_before}\n"
            f"  After:  {files_after}"
        )

        for path, content in disk_before.items():
            assert (project_dir / path).read_bytes() == content, (
                f"Adopt path must not modify on-disk bytes: {path} changed."
            )

    def test_required_packages_deployed_passes_after_lockfile_wipe(
        self,
        hermetic_packaged_sample: HermeticPackagedSample,
    ) -> None:
        """A repaired lockfile lets the next full install pass unchanged."""
        project_dir = hermetic_packaged_sample.project.root
        result1 = hermetic_packaged_sample.run(
            ("install",),
            scenario_id="silent-adopt-policy-initial",
        )
        assert result1.returncode == 0, f"first install: {result1.stderr}\n{result1.stdout}"

        (project_dir / "apm.lock.yaml").unlink()

        result2 = hermetic_packaged_sample.run(
            ("install", "--no-policy"),
            scenario_id="silent-adopt-policy-repair",
        )
        assert result2.returncode == 0, f"re-install: {result2.stderr}\n{result2.stdout}"

        lock2 = _read_lockfile(project_dir)
        files_after = _all_deployed_files(lock2)
        assert files_after, (
            "deployed_files must be repopulated after re-install; "
            "otherwise required-packages-deployed would block the next install (catch-22)."
        )

        result3 = hermetic_packaged_sample.run(
            ("install",),
            scenario_id="silent-adopt-policy-replay",
        )
        assert result3.returncode == 0, (
            "Third install (with policy) must succeed; catch-22 is broken. "
            f"stderr: {result3.stderr}\nstdout: {result3.stdout}"
        )
