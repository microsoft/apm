"""Self-tests for the Mode B silent-extension detector.

Without these, the detector script is itself honor-system: a future
edit could silently break the gate logic and CI would not notice.
These tests pin the script's structural contract -- file presence,
executable bit, critical-path allowlist shape, env-var contract,
and reachability from the CI workflow -- so any regression breaks a
test rather than disabling the gate silently.

The actual gate behaviour (fire on threshold cross, pass on
spec-concurrent edit, respect waiver trailer) is exercised by
invoking the script via subprocess inside a synthetic git repo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DETECTOR = REPO_ROOT / "tests" / "spec_conformance" / "mode_b_detector.sh"
PATHS_FILE = REPO_ROOT / "tests" / "spec_conformance" / "critical_paths.txt"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "spec-conformance.yml"


def test_detector_script_exists_and_is_executable():
    assert DETECTOR.is_file(), f"Mode B detector missing at {DETECTOR}"
    assert os.access(DETECTOR, os.X_OK), (
        f"{DETECTOR} MUST be executable (chmod +x). Without the exec "
        "bit, `bash tests/spec_conformance/mode_b_detector.sh` works "
        "in CI but `./...` invocations regress."
    )


def test_detector_script_passes_bash_syntax_check():
    result = subprocess.run(
        ["bash", "-n", str(DETECTOR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"Mode B detector failed bash -n syntax check:\nstderr: {result.stderr}"
    )


def test_critical_paths_file_lists_known_directories():
    assert PATHS_FILE.is_file(), f"critical_paths.txt missing at {PATHS_FILE}"
    paths = [
        line.strip()
        for line in PATHS_FILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert paths, "critical_paths.txt MUST list at least one path"
    # Every listed path MUST resolve to a real directory in the repo.
    for p in paths:
        resolved = REPO_ROOT / p
        assert resolved.is_dir(), (
            f"critical_paths.txt references {p!r} but {resolved} "
            f"does not exist. Either fix the typo or remove the entry."
        )
    # Sanity: must include the four critical paths named by the
    # original maintainer brief (manifest parser, lockfile writer,
    # resolver, policy engine). We accept the broader allowlist but
    # these four MUST be covered.
    required_substrings = ["primitives", "deps", "policy", "registry"]
    for sub in required_substrings:
        assert any(sub in p for p in paths), (
            f"critical_paths.txt MUST cover {sub!r} (named in the original Mode B brief)"
        )


def test_detector_short_circuits_on_spec_concurrent_edit(tmp_path):
    """A PR that edits the spec body MUST short-circuit (exit 0)."""
    repo = _make_repo(tmp_path)
    # Add a substantive critical-path change AND a spec edit.
    (repo / "src" / "apm_cli" / "deps" / "new.py").write_text(
        "\n".join(f"x = {i}" for i in range(40)) + "\n"
    )
    spec = repo / "docs" / "src" / "content" / "docs" / "specs"
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "openapm-v0.1.md").write_text("placeholder spec edit\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feature + spec edit")
    out = _run_detector(repo)
    assert out.returncode == 0
    assert "spec-concurrent edit detected" in out.stdout, out.stdout


def test_detector_passes_on_out_of_scope_only(tmp_path):
    """A PR that touches nothing under critical paths MUST exit 0."""
    repo = _make_repo(tmp_path)
    (repo / "docs" / "README.md").parent.mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "README.md").write_text("docs edit\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "docs only")
    out = _run_detector(repo)
    assert out.returncode == 0
    assert "no critical-path diff" in out.stdout, out.stdout


def test_detector_fires_on_substantive_critical_path_add(tmp_path):
    """Substantive critical-path add with NO spec edit MUST fire."""
    repo = _make_repo(tmp_path)
    target = repo / "src" / "apm_cli" / "deps" / "new_behaviour.py"
    target.write_text(
        "def new_resolver_branch(x):\n"
        + "\n".join(f"    y_{i} = x + {i}" for i in range(40))
        + "\n    return y_0\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "silent extension under deps/")
    out = _run_detector(repo)
    assert out.returncode == 1, (
        f"detector MUST fire on silent extension; got exit "
        f"{out.returncode}\nstdout: {out.stdout}\nstderr: {out.stderr}"
    )
    assert "Mode B detector" in out.stdout
    assert "apm-spec-waiver" in out.stdout


def test_detector_respects_waiver_trailer(tmp_path):
    """A commit with an `apm-spec-waiver:` trailer MUST pass."""
    repo = _make_repo(tmp_path)
    target = repo / "src" / "apm_cli" / "deps" / "refactor.py"
    target.write_text(
        "def renamed(x):\n"
        + "\n".join(f"    y_{i} = x + {i}" for i in range(40))
        + "\n    return y_0\n"
    )
    _git(repo, "add", "-A")
    _git(
        repo,
        "commit",
        "-m",
        "refactor deps internals\n\napm-spec-waiver: pure refactor, no behaviour delta",
    )
    out = _run_detector(repo)
    assert out.returncode == 0, (
        f"detector MUST accept commit-trailer waiver; got exit "
        f"{out.returncode}\nstdout: {out.stdout}"
    )
    assert "WAIVED" in out.stdout


def test_detector_rejects_short_waiver(tmp_path):
    """A waiver with <16 chars of rationale MUST be rejected."""
    repo = _make_repo(tmp_path)
    target = repo / "src" / "apm_cli" / "deps" / "refactor.py"
    target.write_text(
        "def renamed(x):\n"
        + "\n".join(f"    y_{i} = x + {i}" for i in range(40))
        + "\n    return y_0\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "refactor\n\napm-spec-waiver: tiny")
    out = _run_detector(repo)
    assert out.returncode == 1, "detector MUST reject waivers below the 16-char minimum"


def test_detector_fails_closed_in_ci_on_unresolvable_merge_base(tmp_path):
    """Under CI (GITHUB_ACTIONS=true), an unresolvable merge-base MUST
    fail closed (exit non-zero) instead of skipping. A governance gate
    that cannot evaluate must never pass by luck of the checkout.
    Locally (no GITHUB_ACTIONS), the ergonomic skip (exit 0) is kept."""
    repo = _make_repo(tmp_path)
    target = repo / "src" / "apm_cli" / "deps" / "new_behaviour.py"
    target.write_text(
        "def new_resolver_branch(x):\n"
        + "\n".join(f"    y_{i} = x + {i}" for i in range(40))
        + "\n    return y_0\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "silent extension under deps/")

    # Point BASE at a ref that does not exist so merge-base cannot
    # resolve (the on-demand fetch/unshallow fail against a local repo
    # with no real remote, leaving MB empty).
    base = "origin/does-not-exist"

    ci = _run_detector_with_env(repo, BASE_REF=base, GITHUB_ACTIONS="true")
    assert ci.returncode != 0, (
        "detector MUST fail closed in CI when the merge-base is "
        f"unresolvable; got exit {ci.returncode}\nstdout: {ci.stdout}\n"
        f"stderr: {ci.stderr}"
    )
    assert "cannot resolve merge-base" in ci.stderr, ci.stderr

    local = _run_detector_with_env(repo, BASE_REF=base)
    assert local.returncode == 0, (
        "detector MUST keep the ergonomic skip locally (no "
        f"GITHUB_ACTIONS); got exit {local.returncode}\n"
        f"stdout: {local.stdout}\nstderr: {local.stderr}"
    )
    assert "skipping" in local.stdout, local.stdout


def test_workflow_checkout_uses_full_history():
    """The CI workflow MUST check out full history (fetch-depth: 0) so
    the Mode B detector can deterministically resolve the merge-base.
    Without this, the detector's unresolvable-merge-base branch becomes
    reachable in CI -- the root-cause fail-open hole this gate closes."""
    body = WORKFLOW.read_text()
    assert "fetch-depth: 0" in body, (
        ".github/workflows/spec-conformance.yml MUST set fetch-depth: 0 "
        "on actions/checkout so origin/main is reachable for merge-base"
    )


def test_workflow_invokes_detector_after_orphan_check():
    """The CI workflow MUST wire the detector as a step."""
    body = WORKFLOW.read_text()
    assert "mode_b_detector.sh" in body, (
        ".github/workflows/spec-conformance.yml MUST invoke "
        "tests/spec_conformance/mode_b_detector.sh"
    )
    assert "GH_PR_BODY" in body, (
        "workflow MUST forward PR body to the detector via GH_PR_BODY env var for waiver parsing"
    )
    # Ordering: orphan_check first, detector second, suite third.
    orphan_pos = body.index("orphan_check")
    detector_pos = body.index("mode_b_detector.sh")
    suite_pos = body.index("pytest tests/spec_conformance")
    assert orphan_pos < detector_pos < suite_pos, (
        "workflow ordering MUST be: orphan_check -> mode_b_detector -> conformance suite"
    )


# --- helpers ----------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    )


def _make_repo(tmp_path: Path) -> Path:
    """Build a minimal repo mirroring the parts of the real tree the
    detector inspects, on a `main` branch with a base commit, then
    switch to a feature branch so HEAD..origin/main is meaningful."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main", "--quiet")
    # Mirror the layout the detector references.
    for p in (
        "src/apm_cli/deps",
        "src/apm_cli/primitives",
        "src/apm_cli/policy",
        "src/apm_cli/registry",
        "src/apm_cli/runtime",
        "src/apm_cli/install",
        "src/apm_cli/integration",
        "tests/spec_conformance",
        "docs/src/content/docs/specs",
        "docs/public/specs/manifests",
    ):
        (repo / p).mkdir(parents=True, exist_ok=True)
        (repo / p / ".keep").write_text("")
    # Copy the detector and critical_paths.txt verbatim so we test the
    # actual artifact, not a transcription.
    shutil.copy2(DETECTOR, repo / "tests" / "spec_conformance" / "mode_b_detector.sh")
    (repo / "tests" / "spec_conformance" / "mode_b_detector.sh").chmod(0o755)
    shutil.copy2(PATHS_FILE, repo / "tests" / "spec_conformance" / "critical_paths.txt")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base", "--quiet")
    # Create a feature branch and an origin/main reference the detector
    # can resolve via merge-base.
    _git(repo, "branch", "feature")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "feature", "--quiet")
    return repo


def _run_detector(repo: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "BASE_REF": "origin/main"}
    # Disable the env-var waiver path; we test commit-trailer waivers
    # by leaving GH_PR_BODY unset.
    env.pop("GH_PR_BODY", None)
    return subprocess.run(
        ["bash", "tests/spec_conformance/mode_b_detector.sh"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_detector_with_env(repo: Path, **overrides: str) -> subprocess.CompletedProcess:
    """Run the detector with explicit env overrides. GH_PR_BODY is
    cleared so only the supplied vars drive behaviour."""
    env = {**os.environ}
    env.pop("GH_PR_BODY", None)
    # Strip any ambient GITHUB_ACTIONS so callers control it explicitly.
    env.pop("GITHUB_ACTIONS", None)
    env.update(overrides)
    return subprocess.run(
        ["bash", "tests/spec_conformance/mode_b_detector.sh"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(autouse=True)
def _skip_on_missing_git():
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
