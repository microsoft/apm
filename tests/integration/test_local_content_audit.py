"""End-to-end integration tests for local .apm/ content audit (issue #887).

Exercises the full install + audit round-trip using a real fake APM
project on disk and ``apm`` invoked via subprocess.  Verifies:

* ``apm install`` records local content into the lockfile
  (``local_deployed_files`` / ``local_deployed_file_hashes``).
* ``apm audit --ci`` passes on a clean install and surfaces an
  ``[!]`` advisory for missing ``includes:`` declaration.
* Hash drift on a deployed file is detected and reported as
  ``hash-drift`` with a non-zero exit code.
* Declaring ``includes:`` (auto or explicit list) silences the
  consent advisory.
* ``policy.manifest.require_explicit_includes`` blocks
  ``includes: auto`` via ``apm audit --ci --policy <file>``.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def apm_command():
    """Resolve the ``apm`` CLI executable for subprocess invocation."""
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


def _write_manifest(project: Path, *, includes=None) -> None:
    """Write a minimal apm.yml. ``includes`` may be None, 'auto', or a list."""
    data = {
        "name": "audit-fixture",
        "version": "0.1.0",
        "description": "Fixture project for local-content audit integration tests",
    }
    if includes is not None:
        data["includes"] = includes
    (project / "apm.yml").write_text(yaml.dump(data, sort_keys=False))


def _seed_local_content(project: Path) -> None:
    """Create a SKILL.md and an instruction file under .apm/."""
    skill_dir = project / ".apm" / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\ndescription: foo skill for tests\n---\n# Foo\nbody\n")

    instr_dir = project / ".apm" / "instructions"
    instr_dir.mkdir(parents=True)
    (instr_dir / "bar.instructions.md").write_text("---\napplyTo: '**'\n---\n# Bar\nbody\n")

    # .github/ already-exists triggers copilot target detection (and the
    # default fallback is also copilot, so this is belt-and-suspenders).
    (project / ".github").mkdir()


def _make_project(tmp_path: Path, *, includes=None) -> Path:
    """Build a fake APM project rooted at ``tmp_path/proj``."""
    project = tmp_path / "proj"
    project.mkdir()
    _write_manifest(project, includes=includes)
    _seed_local_content(project)
    return project


def _run_apm(apm_command: str, args, cwd: Path):
    """Invoke the apm CLI and return CompletedProcess."""
    return subprocess.run(
        [apm_command, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _install(apm_command: str, project: Path):
    result = _run_apm(apm_command, ["install"], project)
    assert result.returncode == 0, (
        f"apm install failed (exit {result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    return result


def _audit_json(apm_command: str, project: Path, extra_args=()):
    """Run ``apm audit --ci -f json`` and return (exit_code, parsed_json)."""
    args = ["audit", "--ci", "--no-policy", "-f", "json", *extra_args]
    if "--policy" in extra_args:
        # When policy is provided, drop --no-policy so the override wins.
        args = ["audit", "--ci", "-f", "json", *extra_args]
    result = _run_apm(apm_command, args, project)
    # JSON output is on stdout; tolerate trailing log lines.
    payload = None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Some checks may print warnings on stdout before the JSON body.
        # Try to locate the first '{' and parse from there.
        idx = result.stdout.find("{")
        if idx >= 0:
            payload = json.loads(result.stdout[idx:])
    assert payload is not None, (
        f"audit JSON parse failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    return result.returncode, payload, result


def _check(payload: dict, name: str) -> dict:
    for c in payload["checks"]:
        if c["name"] == name:
            return c
    raise AssertionError(
        f"check '{name}' not found in payload (have: {[c['name'] for c in payload['checks']]})"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLocalContentAudit:
    """End-to-end coverage for issue #887 close-the-gap behavior."""

    def test_install_records_self_entry(self, tmp_path, apm_command):
        """Test A: ``apm install`` records local files + hashes in lockfile."""
        project = _make_project(tmp_path)
        _install(apm_command, project)

        lock_path = project / "apm.lock.yaml"
        assert lock_path.exists(), "apm.lock.yaml not created by install"

        with open(lock_path) as f:
            lock = yaml.safe_load(f)

        deployed = lock.get("local_deployed_files") or []
        hashes = lock.get("local_deployed_file_hashes") or {}

        assert deployed, f"local_deployed_files empty: {lock!r}"
        assert hashes, f"local_deployed_file_hashes empty: {lock!r}"

        # Both seeded primitives should be deployed under .github/ (copilot
        # target).  Skill goes to .github/skills/foo/, instruction to
        # .github/instructions/bar.instructions.md.
        assert any("instructions/bar.instructions.md" in p for p in deployed), (
            f"instruction not deployed: {deployed}"
        )
        assert any("skills/foo" in p for p in deployed), f"skill not deployed: {deployed}"

        # The instruction file (a regular file) must have a hash entry.
        instr_keys = [k for k in hashes if k.endswith("bar.instructions.md")]
        assert instr_keys, f"no hash recorded for bar.instructions.md: {list(hashes)}"
        assert hashes[instr_keys[0]].startswith("sha256:"), (
            f"hash not sha256-prefixed: {hashes[instr_keys[0]]!r}"
        )

    def test_audit_passes_clean_install(self, tmp_path, apm_command):
        """Test B: clean install passes audit; consent advisory present."""
        project = _make_project(tmp_path)  # no includes: declared
        _install(apm_command, project)

        exit_code, payload, _ = _audit_json(apm_command, project)
        assert exit_code == 0, f"audit --ci failed on clean install: {payload}"
        assert payload["passed"] is True

        ci = _check(payload, "content-integrity")
        assert ci["passed"] is True, f"content-integrity not passing: {ci}"

        consent = _check(payload, "includes-consent")
        # Advisory is encoded in the raw message text; the JSON payload does
        # not guarantee CLI-style status markers such as [!].
        assert consent["passed"] is True
        assert "includes:" in consent["message"], (
            f"expected includes guidance in includes-consent message, got: {consent['message']!r}"
        )
        assert "includes: auto" in consent["message"], (
            f"expected auto-includes advisory in includes-consent message, "
            f"got: {consent['message']!r}"
        )

    def test_audit_detects_drift(self, tmp_path, apm_command):
        """Test C: hand-edit a deployed file -> hash-drift detected."""
        project = _make_project(tmp_path)
        _install(apm_command, project)

        # Tamper with the deployed copy under .github/.
        deployed_instr = project / ".github" / "instructions" / "bar.instructions.md"
        assert deployed_instr.exists(), f"target file missing post-install: {deployed_instr}"
        with open(deployed_instr, "a") as f:
            f.write("\nTAMPERED\n")

        exit_code, payload, result = _audit_json(apm_command, project)
        assert exit_code != 0, (
            f"audit --ci should fail on drift but passed: {payload}\nSTDERR: {result.stderr}"
        )
        ci = _check(payload, "content-integrity")
        assert ci["passed"] is False, f"content-integrity should fail: {ci}"
        # Either the message or details should reference hash-drift and the
        # path of the modified file.
        haystack = ci["message"] + " " + " ".join(ci.get("details") or [])
        assert "hash-drift" in haystack, f"'hash-drift' not in failure output: {haystack!r}"
        assert "bar.instructions.md" in haystack, (
            f"path of modified file not surfaced: {haystack!r}"
        )

    def test_audit_passes_with_explicit_includes(self, tmp_path, apm_command):
        """Test D: declaring ``includes:`` removes the consent advisory."""
        # Use 'auto' first.
        project = _make_project(tmp_path, includes="auto")
        _install(apm_command, project)

        exit_code, payload, _ = _audit_json(apm_command, project)
        assert exit_code == 0, f"audit failed unexpectedly: {payload}"

        consent = _check(payload, "includes-consent")
        assert consent["passed"] is True
        assert "[!]" not in consent["message"], (
            f"unexpected '[!]' advisory when includes is declared: {consent['message']!r}"
        )

        # Also verify the explicit-list form by rewriting and re-auditing.
        _write_manifest(
            project,
            includes=[
                ".apm/skills/foo/SKILL.md",
                ".apm/instructions/bar.instructions.md",
            ],
        )
        exit_code2, payload2, _ = _audit_json(apm_command, project)
        assert exit_code2 == 0, f"audit failed with explicit list: {payload2}"
        consent2 = _check(payload2, "includes-consent")
        assert "[!]" not in consent2["message"]

    def test_policy_blocks_undeclared_includes(self, tmp_path, apm_command):
        """Test E: ``require_explicit_includes`` blocks ``includes: auto``."""
        project = _make_project(tmp_path, includes="auto")
        _install(apm_command, project)

        # Local policy file -- pass via --policy <path>.
        policy_path = project / "apm-policy.yml"
        policy_path.write_text(
            yaml.dump(
                {
                    "name": "test-explicit-includes",
                    "enforcement": "block",
                    "manifest": {"require_explicit_includes": True},
                },
                sort_keys=False,
            )
        )

        exit_code, payload, result = _audit_json(
            apm_command, project, extra_args=["--policy", str(policy_path)]
        )
        assert exit_code != 0, (
            f"policy should have blocked includes: auto but passed:\n"
            f"{payload}\nSTDERR:\n{result.stderr}"
        )
        check = _check(payload, "explicit-includes")
        assert check["passed"] is False
        # Message should mention the policy requirement.
        assert "explicit" in check["message"].lower()
        assert "includes" in check["message"].lower()
