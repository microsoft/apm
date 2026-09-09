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
import sys
from pathlib import Path

import pytest

from tests.spec_conformance._manifest import (
    MANIFEST_PATH,
    REPO_ROOT,
    SCHEMA_PATH,
    SPEC_PATH,
)

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


@pytest.mark.parametrize("selected_path", [SPEC_PATH, MANIFEST_PATH])
def test_detector_short_circuits_on_spec_concurrent_edit(tmp_path, selected_path):
    """A PR that edits the spec body MUST short-circuit (exit 0)."""
    repo = _make_repo(tmp_path)
    # Add a substantive critical-path change AND a spec edit.
    (repo / "src" / "apm_cli" / "deps" / "new.py").write_text(
        "\n".join(f"x = {i}" for i in range(40)) + "\n"
    )
    selected = repo / selected_path.relative_to(REPO_ROOT)
    selected.write_text(selected.read_text() + "\n# Informative draft note\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feature + spec edit")
    out = _run_detector(repo)
    assert out.returncode == 0
    assert "spec-concurrent edit detected" in out.stdout, out.stdout


def test_retained_previous_minor_does_not_satisfy_active_spec_citation(tmp_path):
    """Changing only the previous minor cannot cite the active assessment."""
    repo = _make_repo(tmp_path)
    (repo / "src/apm_cli/deps/new.py").write_text("\n".join(f"x = {i}" for i in range(40)) + "\n")
    (repo / "docs/src/content/docs/specs/openapm-v0.1.md").write_text("old history\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "critical change with old-only notice")
    result = _run_detector(repo)
    assert result.returncode == 1
    assert "Mode B detector" in result.stdout


def test_detector_short_circuits_on_new_top_level_req_marker(tmp_path):
    """A new column-0 ``@pytest.mark.req`` marker MUST short-circuit (exit 0).

    Citing an EXISTING requirement with a new marker is a first-class way to
    satisfy Mode B -- the detector's own message lists it, and orphan_check
    then owns correctness. Every test in this suite declares its marker at
    module level, so this is the common shape; when it fails to register, a
    legitimately-cited PR is pushed toward an unnecessary spec amendment or a
    waiver that would misrepresent a behaviour change as a refactor.
    """
    repo = _make_repo(tmp_path)
    (repo / "src" / "apm_cli" / "deps" / "new.py").write_text(
        "\n".join(f"x = {i}" for i in range(40)) + "\n"
    )
    (repo / "tests" / "spec_conformance" / "test_new_citation.py").write_text(
        'import pytest\n\n\n@pytest.mark.req("req-lk-012")\ndef test_cited():\n    assert True\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feature + citation via new req marker")
    out = _run_detector(repo)
    assert out.returncode == 0, (
        f"a new top-level req marker MUST short-circuit; got exit "
        f"{out.returncode}\nstdout: {out.stdout}\nstderr: {out.stderr}"
    )
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


def test_detector_passes_when_critical_diff_has_no_substantive_additions(tmp_path):
    """A comment-only critical-path diff MUST report zero and exit 0."""
    repo = _make_repo(tmp_path)
    target = repo / "src" / "apm_cli" / "install" / ".keep"
    target.write_text("# Clarify existing behavior.\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "clarify install comment")

    out = _run_detector(repo)

    assert out.returncode == 0, (
        f"comment-only diff MUST pass; got exit {out.returncode}\n"
        f"stdout: {out.stdout}\nstderr: {out.stderr}"
    )
    assert "0 substantive added lines" in out.stdout, out.stdout


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


def test_detector_controls_ignore_ambient_gate_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host's gate configuration must not change the synthetic control."""
    repo = _make_repo(tmp_path)
    (repo / "src/apm_cli/deps/new.py").write_text("\n".join(f"x = {i}" for i in range(40)) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "uncited critical change")
    for name, value in {
        "BASE_REF": "missing-base",
        "MODE_B_THRESHOLD": "99999",
        "GH_PR_BODY": "apm-spec-waiver: ambient waiver must not apply",
        "GITHUB_ACTIONS": "true",
    }.items():
        monkeypatch.setenv(name, value)
    result = _run_detector(repo)
    assert result.returncode == 1
    assert "Mode B detector" in result.stdout


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


def test_workflow_triggers_on_public_spec_assets():
    assert "'docs/public/specs/**'" in WORKFLOW.read_text()


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
    for relative in ("tests/__init__.py", "tests/spec_conformance/__init__.py"):
        (repo / relative).write_text("")
    shutil.copy2(
        REPO_ROOT / "tests/spec_conformance/_manifest.py",
        repo / "tests/spec_conformance/_manifest.py",
    )
    for source in (SPEC_PATH, MANIFEST_PATH, SCHEMA_PATH):
        destination = repo / source.relative_to(REPO_ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base", "--quiet")
    # Create a feature branch and an origin/main reference the detector
    # can resolve via merge-base.
    _git(repo, "branch", "feature")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "feature", "--quiet")
    return repo


def _run_detector(repo: Path) -> subprocess.CompletedProcess:
    return _run_detector_with_env(repo, BASE_REF="origin/main")


def _run_detector_with_env(repo: Path, **overrides: str) -> subprocess.CompletedProcess:
    """Only explicit overrides may configure the synthetic detector."""
    env = {**os.environ, "PYTHON": sys.executable, "PYTHONPATH": str(repo)}
    for name in ("GH_PR_BODY", "GITHUB_ACTIONS", "BASE_REF", "MODE_B_THRESHOLD"):
        env.pop(name, None)
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
