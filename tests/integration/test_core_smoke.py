"""Tier-2 smoke tests against the built apm binary.

Purpose
-------
Fail fast in the merge queue BEFORE the 30-minute heavy integration
suite runs, by exercising the CLI surface that the README's "three
promises" actually advertise:

    1. Portable by manifest -- ``apm init``, ``apm install``,
       ``apm compile``.
    2. Secure by default    -- ``apm audit``.
    3. Governed by policy   -- ``apm policy status`` runs discovery.

Scope rules
-----------
- Hermetic: NO network calls. No GitHub API, no marketplace fetch,
  no runtime-binary install. The fixture project declares zero
  remote dependencies so ``apm install`` exercises the install
  pipeline as a no-op rather than going to the network.
- Fast: the whole module must run in well under a minute on a fresh
  GitHub-hosted runner. Each subprocess uses a 60-second cap.
- Sanity, not coverage: this module deliberately does NOT verify
  exact compiled paths, target-detection branching, or policy
  enforcement semantics. Those belong in the heavy integration
  suite. The smoke job just answers "does the binary start, does
  each core command pipeline run end-to-end and exit cleanly".
- Aligned to README: every test in this module maps to one of the
  three promises in ``README.md``. ``apm run`` and ``apm runtime``
  are explicitly experimental (``--help`` text says so) and live
  in ``test_runtime_smoke.py`` under ``requires_runtime_*`` markers
  in the heavy suite, not here.

Markers
-------
- ``requires_e2e_mode`` -- gates on ``APM_E2E_TESTS=1`` so a casual
  ``pytest tests/integration/`` does not shell out.
- ``requires_apm_binary`` -- gates on the resolved binary path so
  the module is skipped (not failed) on a contributor laptop that
  has no local build.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]


SMOKE_TIMEOUT_SECONDS = 60


def _run_apm(
    apm_binary_path: Path,
    args: list[str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Invoke the resolved apm binary with a hard timeout.

    Centralized so every smoke test enforces the same timeout and
    surfaces stdout/stderr identically when an assertion fails.
    """
    return subprocess.run(
        [str(apm_binary_path), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=SMOKE_TIMEOUT_SECONDS,
        check=False,
    )


@pytest.fixture
def smoke_project(tmp_path: Path, apm_binary_path: Path) -> Path:
    """Materialize a hermetic apm project with one local instruction.

    The project declares zero remote dependencies so ``apm install``
    is a network-free no-op, and ships one ``.apm/instructions/``
    file so ``apm compile`` has real input to fan out.
    """
    project_dir = tmp_path / "smoke-fixture"
    result = _run_apm(
        apm_binary_path,
        ["init", "smoke-fixture", "-y", "--target", "copilot"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, (
        f"apm init failed (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert project_dir.exists(), "apm init did not create project directory"

    instructions_dir = project_dir / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True, exist_ok=True)
    (instructions_dir / "style.instructions.md").write_text(
        "---\n"
        'description: "Smoke test fixture instruction."\n'
        'applyTo: "**"\n'
        "---\n"
        "# Style\n"
        "Use ASCII only.\n",
        encoding="utf-8",
    )

    return project_dir


class TestBinaryStartup:
    """Sanity: the built binary starts and reports its version."""

    def test_apm_version_runs(self, apm_binary_path: Path, tmp_path: Path) -> None:
        """``apm --version`` must exit 0 and print non-empty output.

        This is the cheapest possible signal that the PyInstaller
        binary is intact (no missing imports, no tomllib breakage,
        no platform mismatch). If this test fails, every downstream
        smoke or integration check would also fail; failing fast
        here saves merge-queue minutes.
        """
        result = _run_apm(apm_binary_path, ["--version"], cwd=tmp_path)
        assert result.returncode == 0, (
            f"apm --version failed (rc={result.returncode})\nstderr:\n{result.stderr}"
        )
        assert result.stdout.strip(), "apm --version produced empty stdout"


class TestPortableByManifest:
    """README promise 1: portable by manifest.

    The manifest pipeline is what users hit on every project: init
    scaffolds the manifest, install resolves it, compile distributes
    the resolved primitives to per-target surfaces. All three must
    exit cleanly on a hermetic fixture for the build to be shippable.
    """

    def test_init_scaffolds_manifest(self, smoke_project: Path) -> None:
        """``apm init`` must materialize a parseable ``apm.yml``.

        The fixture itself is produced by ``apm init`` (see the
        ``smoke_project`` fixture); this test just asserts the
        post-condition the README's quickstart relies on.
        """
        manifest = smoke_project / "apm.yml"
        assert manifest.is_file(), "apm init did not produce apm.yml"
        content = manifest.read_text(encoding="utf-8")
        assert "name:" in content, "apm.yml missing 'name:' key"
        assert "smoke-fixture" in content, (
            "apm.yml does not contain the project name passed to init"
        )

    def test_install_pipeline_runs(self, smoke_project: Path, apm_binary_path: Path) -> None:
        """``apm install`` must succeed on a zero-dependency manifest.

        Even with no remote deps to fetch, install still walks the
        manifest, computes the dependency graph, and writes / refreshes
        ``apm.lock.yaml``. A non-zero exit here means the install
        pipeline is broken regardless of network connectivity.
        """
        result = _run_apm(apm_binary_path, ["install"], cwd=smoke_project)
        assert result.returncode == 0, (
            f"apm install failed (rc={result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_compile_pipeline_produces_output(
        self, smoke_project: Path, apm_binary_path: Path
    ) -> None:
        """``apm compile -t copilot`` must emit a generated file.

        With one local instruction in ``.apm/instructions/``, compile
        must walk the primitive tree, render at least one output file
        carrying the APM build-ID marker, and exit 0. We check for the
        marker rather than a specific path because target routing can
        legitimately land output in either ``.github/`` or ``AGENTS.md``
        depending on detected signals; the smoke contract is "compile
        ran end-to-end and wrote something".
        """
        result = _run_apm(apm_binary_path, ["compile", "-t", "copilot"], cwd=smoke_project)
        assert result.returncode == 0, (
            f"apm compile failed (rc={result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        candidates = [
            smoke_project / ".github" / "copilot-instructions.md",
            smoke_project / "AGENTS.md",
        ]
        produced = [p for p in candidates if p.is_file()]
        assert produced, (
            "apm compile did not produce any of the expected output files: "
            f"{[str(p) for p in candidates]}"
        )
        marker = "Generated by APM CLI"
        for path in produced:
            content = path.read_text(encoding="utf-8")
            assert marker in content, f"compiled file {path} missing APM generation marker"


class TestSecureByDefault:
    """README promise 2: secure by default.

    The audit pipeline is APM's headline security gesture (Unicode
    scan, lockfile integrity, drift detection). The smoke check is
    that the pipeline is reachable and exits cleanly on a clean
    fixture; concrete detection semantics are exercised in the
    audit-specific integration tests.
    """

    def test_audit_pipeline_runs(self, smoke_project: Path, apm_binary_path: Path) -> None:
        """``apm audit`` must exit 0 on a clean fixture.

        The fixture has no remote deps and no installed packages,
        so audit has nothing to flag. A non-zero exit would indicate
        the audit pipeline itself is broken (not that something was
        flagged), which is exactly the failure mode worth catching
        before the heavy suite runs.
        """
        # apm install must run first to materialize apm.lock.yaml,
        # which is audit's primary input. Install's own success is
        # asserted in TestPortableByManifest above; here we only
        # need it as setup.
        install = _run_apm(apm_binary_path, ["install"], cwd=smoke_project)
        assert install.returncode == 0, "apm install (audit precondition) failed: " + install.stderr

        result = _run_apm(apm_binary_path, ["audit"], cwd=smoke_project)
        assert result.returncode == 0, (
            f"apm audit failed (rc={result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


class TestGovernedByPolicy:
    """README promise 3: governed by policy.

    Concrete enforcement semantics (deny rules blocking installs,
    bypass tokens, signature verification) are exercised against
    configured fixtures in the heavy suite. The smoke check runs
    the policy DISCOVERY pipeline end-to-end -- git-remote probing,
    org resolution, cache lookup, rule evaluation -- and asserts
    the diagnostic surface renders the result. A regression in any
    layer of the policy stack (entry point, lazy imports inside
    the PyInstaller bundle, discovery code path, status renderer)
    surfaces here.
    """

    def test_policy_status_runs_discovery(self, smoke_project: Path, apm_binary_path: Path) -> None:
        """``apm policy status`` must run the discovery pipeline and exit 0.

        On the hermetic fixture (no git remote configured), the
        discovery layer is expected to gracefully report
        ``no_git_remote`` rather than crash; the status table itself
        must render. This exercises far more of the governance stack
        than ``--help`` would: Click entry point + policy module +
        discovery + cache + rule evaluator + status renderer all
        execute on the real binary.
        """
        result = _run_apm(apm_binary_path, ["policy", "status"], cwd=smoke_project)
        assert result.returncode == 0, (
            f"apm policy status failed (rc={result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        stdout_lower = result.stdout.lower()
        assert "policy status" in stdout_lower, (
            "apm policy status output missing the 'Policy Status' header; "
            "the diagnostic renderer did not run"
        )
        assert "outcome" in stdout_lower, (
            "apm policy status output missing the 'Outcome' field; "
            "the discovery layer did not report a result"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
