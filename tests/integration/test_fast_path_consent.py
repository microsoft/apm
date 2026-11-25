"""The install fast path never migrates personal consent (#5428), over the real CLI.

PROMISE: P-INV-I5 -- a command writes only its declared write set: the fast
path's speculative decision reads a legacy ``$APM_HOME/approvals.yml`` and never
folds it into ``config.json`` or deletes it; the full install owns that write.

The real CLI over the tree-10 offline world (a dependency-declared MCP server):
``APM_INSTALL_FAST_PATH=decide`` reports the decision and writes nothing, so
the legacy file and ``config.json`` stay byte-identical; with every fast path
off, the same state migrates (positive control: the legacy file is live).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import apm_cli
from apm_cli.install.fast_entry import DECIDE_EXIT
from tests.fixtures.perf import TREE_RECIPES, build_tree, serve_offline

pytestmark = [
    pytest.mark.component,
    pytest.mark.integration,
    pytest.mark.promise("P-INV-I5"),
]

_NO_HIT = ("CI", "GITHUB_ACTIONS", "TF_BUILD", "BUILD_ID", "GITLAB_CI", "BUILDKITE")
# The console script's own entry (pyproject: apm = apm_cli.entrypoint:main).
_ENTRY = (sys.executable, "-c", "import sys; from apm_cli.entrypoint import main; sys.exit(main())")
_LEGACY = "perf-org/pkg-000: {hooks: true}\n"


@pytest.fixture
def world():
    """``(install, apm_home)`` in a fresh, installed tree-10 project."""
    work = Path(tempfile.mkdtemp(prefix="apm-consent-"))
    try:
        tree = build_tree(TREE_RECIPES["tree-10"], work / "tree")
        with serve_offline(tree, work / "world") as served:
            project = work / "project"
            shutil.copytree(tree.project, project)
            env = {k: v for k, v in served.env.items() if k not in _NO_HIT}
            env["PYTHONPATH"] = str(Path(apm_cli.__file__).resolve().parents[1])

            def install(fast: str) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [*_ENTRY, "install"],
                    cwd=project,
                    env=dict(env, APM_INSTALL_FAST_PATH=fast),
                    capture_output=True,
                    text=True,
                    timeout=600,
                )

            assert install("0").returncode == 0
            yield install, Path(served.apm_home)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _consent(home: Path) -> tuple[bytes | None, bytes | None]:
    return tuple(  # type: ignore[return-value]
        path.read_bytes() if path.exists() else None
        for path in (home / "approvals.yml", home / "config.json")
    )


def test_a_fast_path_decision_never_migrates_a_legacy_approvals_file(world) -> None:
    install, home = world
    (home / "approvals.yml").write_text(_LEGACY, encoding="utf-8")
    before = _consent(home)

    decided = install("decide")

    # The full fast decision was reached (it reads consent on the way); an early
    # decline (no lockfile, an error) would leave the files alone for another reason.
    assert decided.returncode == DECIDE_EXIT, decided.stdout + decided.stderr
    assert "install fast path: no-op (desired state already deployed)" in decided.stderr
    _assert_consent(home, before)


def test_the_full_install_still_migrates_a_legacy_approvals_file(world) -> None:
    """Positive control: the legacy file is live, and the full install folds it in."""
    install, home = world
    (home / "approvals.yml").write_text(_LEGACY, encoding="utf-8")

    done = install("0")

    assert done.returncode == 0, done.stdout + done.stderr
    assert not (home / "approvals.yml").exists()
    assert "perf-org/pkg-000" in (home / "config.json").read_text(encoding="utf-8")


def _assert_consent(home: Path, before) -> None:
    assert _consent(home) == before, "the fast path migrated personal consent"
