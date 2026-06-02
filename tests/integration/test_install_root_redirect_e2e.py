"""End-to-end coverage for ``apm install --root DIR``.

Companion to ``test_compile_root_redirect_e2e``. Where the compile test
pins the byte-identical artifact promise, this test pins the *resolver*
half of the ``--root`` contract (issue #888): when the source-root
override is active, dependency manifests resolve from ``$PWD`` while the
``apm_modules/`` tree, lockfile, and integrated harness files are all
written under ``DIR``.

This is the ``source_root != project_root`` path the review panel
flagged as uncovered: with no override, the resolver anchors the
manifest read on the (cwd-equal) project root; only under ``--root``
does the manifest anchor diverge to ``$PWD`` while writes follow the
chdir into ``DIR``.

A local ``path:`` dependency keeps the test hermetic -- no network, no
git, no registry.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_INSTALL = [sys.executable, "-m", "apm_cli.cli", "install"]


def _make_local_dep(pkg: Path) -> None:
    instructions = pkg / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (pkg / "apm.yml").write_text(
        "name: dep-pkg\ndescription: local dep\nversion: 0.0.1\n",
        encoding="utf-8",
    )
    (instructions / "dep.instructions.md").write_text(
        '---\ndescription: Dep guide\napplyTo: "**"\n---\n\n# Dep\n\nFrom the dependency.\n',
        encoding="utf-8",
    )


def test_install_root_resolves_sources_from_pwd_and_writes_under_root(
    tmp_path: Path,
) -> None:
    pkg = tmp_path / "pkg"
    _make_local_dep(pkg)

    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "apm.yml").write_text(
        "name: consumer\n"
        "description: consumer\n"
        "version: 0.0.1\n"
        "dependencies:\n"
        "  apm:\n"
        f"    - path: {pkg}\n",
        encoding="utf-8",
    )

    deploy = tmp_path / "deploy"
    deploy.mkdir()

    result = subprocess.run(
        [*_INSTALL, "--root", str(deploy), "--target", "copilot"],
        cwd=str(consumer),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    # Writes landed under the deploy root: lockfile, modules tree, and the
    # integrated harness file all live in ``deploy``.
    assert (deploy / "apm.lock.yaml").exists(), "lockfile not written under --root"
    assert (deploy / "apm_modules" / "_local" / "pkg").is_dir(), (
        "materialised dependency not written under --root"
    )
    assert (deploy / ".github" / "instructions" / "dep.instructions.md").exists(), (
        "integrated harness file not written under --root"
    )

    # The source tree stays clean: the resolver read the manifest from
    # ``$PWD`` but never wrote install artifacts back into it.
    assert not (consumer / "apm_modules").exists(), "apm_modules leaked into $PWD"
    assert not (consumer / "apm.lock.yaml").exists(), "lockfile leaked into $PWD"
    assert not (consumer / ".github").exists(), "harness files leaked into $PWD"
