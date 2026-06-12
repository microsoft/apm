"""End-to-end coverage of every user-visible promise of ``apm deps why``.

Each test name reads as the promise sentence it defends. The pipeline
exercised is real: build an ``apm.lock.yaml`` on disk under ``tmp_path``,
invoke the CLI through ``CliRunner``, and assert against the actual
human / JSON output the user sees.

These tests are complementary to ``tests/unit/commands/deps/test_why_command.py``
(unit-level argv + branch coverage) and ``tests/integration/test_deps_why_e2e.py``
(the original happy-path e2e). They exist to give every user-promise its
own integration-tier regression trap so that silent drift in the
file-load -> walker -> renderer chain fails loudly here before a user
notices.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LOCKFILE_NAME, LockedDependency, LockFile

# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _cwd(path: Path):
    original = os.getcwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(original)


def _write_project_lockfile(project_root: Path, deps: list[LockedDependency]) -> Path:
    lf = LockFile()
    for dep in deps:
        lf.add_dependency(dep)
    lockfile_path = project_root / LOCKFILE_NAME
    lockfile_path.write_text(lf.to_yaml(), encoding="utf-8")
    return lockfile_path


@pytest.fixture
def runner() -> CliRunner:
    # Click 8.2+ CliRunner exposes stdout and stderr as separate streams
    # by default; Promise C relies on that separation.
    return CliRunner()


@pytest.fixture
def linear_chain_project(tmp_path: Path) -> Path:
    """A linear 3-level chain: big -> shared -> deep."""
    _write_project_lockfile(
        tmp_path,
        [
            LockedDependency(
                repo_url="acme/big-skills", version="1.2.4", depth=1, resolved_by=None
            ),
            LockedDependency(
                repo_url="acme/shared-utils",
                version="1.4.2",
                depth=2,
                resolved_by="acme/big-skills",
            ),
            LockedDependency(
                repo_url="acme/deep-core",
                version="3.1.0",
                depth=3,
                resolved_by="acme/shared-utils",
            ),
        ],
    )
    return tmp_path


@pytest.fixture
def shared_transitive_project(tmp_path: Path) -> Path:
    """Two declared roots; one shared transitive recorded under a single
    parent (the resolver collapses to one ``resolved_by`` per record --
    documented in docs/.../manage-dependencies.md as "one root-to-target
    chain, not a fan-out")."""
    _write_project_lockfile(
        tmp_path,
        [
            LockedDependency(
                repo_url="acme/big-skills", version="1.2.4", depth=1, resolved_by=None
            ),
            LockedDependency(
                repo_url="acme/other-skills",
                version="0.9.1",
                depth=1,
                resolved_by=None,
            ),
            LockedDependency(
                repo_url="acme/shared-utils",
                version="1.4.2",
                depth=2,
                resolved_by="acme/big-skills",
            ),
        ],
    )
    return tmp_path


@pytest.fixture
def truncated_chain_project(tmp_path: Path) -> Path:
    """Lockfile records a parent that is itself missing -- simulates
    partial/corrupt data the walker must defend against."""
    _write_project_lockfile(
        tmp_path,
        [
            LockedDependency(
                repo_url="acme/orphan",
                version="1.0.0",
                depth=2,
                resolved_by="acme/missing-parent",
            ),
        ],
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Promise A: human-readable chain renders root -> target
# ---------------------------------------------------------------------------


def test_promise_a_apm_deps_why_prints_human_chain_from_root_to_target(
    runner, linear_chain_project: Path
):
    """Promise A: ``apm deps why <pkg>`` prints the human-readable chain
    from the direct root in apm.yml all the way to the queried package."""
    with _cwd(linear_chain_project):
        result = runner.invoke(cli, ["deps", "why", "deep-core"])
    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert "acme/deep-core@3.1.0" in out
    assert "(transitive)" in out
    root_idx = out.index("acme/big-skills")
    mid_idx = out.index("acme/shared-utils")
    leaf_idx = out.index("acme/deep-core", root_idx + 1)
    assert root_idx < mid_idx < leaf_idx, f"chain must render root-to-target order; got:\n{out}"
    assert "declared in apm.yml" in out
    assert "+--" in out


# ---------------------------------------------------------------------------
# Promise B: exit codes
# ---------------------------------------------------------------------------


def test_promise_b_exit_0_on_success_and_nonzero_with_clear_error_when_pkg_missing(
    runner, linear_chain_project: Path
):
    """Promise B: success exits 0; missing package exits nonzero with a
    clear, named error message."""
    with _cwd(linear_chain_project):
        ok = runner.invoke(cli, ["deps", "why", "deep-core"])
        missing = runner.invoke(cli, ["deps", "why", "nope-not-here"])
    assert ok.exit_code == 0, ok.stderr
    assert missing.exit_code != 0
    assert missing.exit_code == 1
    combined = (missing.stdout or "") + (missing.stderr or "")
    assert "not installed" in combined
    assert "nope-not-here" in combined


# ---------------------------------------------------------------------------
# Promise C: --json stream discipline (regression-trap)
# ---------------------------------------------------------------------------


def test_promise_c_json_mode_emits_valid_json_to_stdout_and_errors_to_stderr(
    runner, linear_chain_project: Path, tmp_path: Path
):
    """Promise C: ``--json`` mode emits a single valid JSON document to
    stdout on success; on failure stdout stays clean (jq-safe) and the
    JSON error envelope is on stderr."""
    with _cwd(linear_chain_project):
        ok = runner.invoke(cli, ["deps", "why", "deep-core", "--json"])
    assert ok.exit_code == 0, ok.stderr
    # stdout must parse as a single JSON document.
    payload = json.loads(ok.stdout)
    assert payload["package"]["repo_url"] == "acme/deep-core"

    # Failure path: no lockfile -> stdout MUST stay empty, stderr carries
    # the JSON error envelope. This is the panel-flagged stream-discipline
    # regression trap; at integration tier (not unit-mocked) it would
    # catch a regression like dropping ``set_console_stderr(True)``.
    empty_project = tmp_path / "empty"
    empty_project.mkdir()
    with _cwd(empty_project):
        missing = runner.invoke(cli, ["deps", "why", "anything", "--json"])
    assert missing.exit_code == 2
    assert missing.stdout == "", (
        f"stdout must stay clean under --json failure; got: {missing.stdout!r}"
    )
    err_payload = json.loads(missing.stderr.strip().splitlines()[-1])
    assert err_payload == {"error": "no_lockfile"}


# ---------------------------------------------------------------------------
# Promise D: constraint annotations (present + graceful absence)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_constraint", [True, False])
def test_promise_d_constraint_annotation_shows_when_present_and_degrades_gracefully(
    runner, linear_chain_project: Path, monkeypatch, with_constraint: bool
):
    """Promise D: when ``LockedDependency.constraint`` is populated
    (post-#1488), the renderer surfaces it in both human and JSON output;
    when absent (current main), output must still render correctly with
    a plain "declared in apm.yml" annotation -- no AttributeError, no
    missing chain elements."""
    if with_constraint:
        # Inject constraints via the walker's getattr indirection: the
        # walker reads dep.constraint with getattr(..., None), and the
        # renderer formats it. Patching here proves the end-to-end
        # rendering pipeline picks up the new field once #1488 lands.
        constraints = {
            "acme/big-skills": "^1.2.0",
            "acme/shared-utils": "^1.4.0",
        }
        import apm_cli.deps.why_walker as walker_mod

        original = walker_mod._dep_constraint
        monkeypatch.setattr(
            walker_mod,
            "_dep_constraint",
            lambda dep: constraints.get(dep.repo_url, original(dep)),
        )

    with _cwd(linear_chain_project):
        human = runner.invoke(cli, ["deps", "why", "deep-core"])
        as_json = runner.invoke(cli, ["deps", "why", "deep-core", "--json"])

    assert human.exit_code == 0, human.stderr
    assert as_json.exit_code == 0, as_json.stderr
    payload = json.loads(as_json.stdout)
    root_edge = payload["paths"][0]["chain"][0]

    if with_constraint:
        assert "constraint: ^1.2.0" in human.stdout
        assert root_edge["constraint"] == "^1.2.0"
    else:
        assert "constraint:" not in human.stdout
        assert root_edge["constraint"] is None

    # Either way the root edge is annotated as the declared one.
    assert "declared in apm.yml" in human.stdout
    assert root_edge["is_direct"] is True


# ---------------------------------------------------------------------------
# Promise E: shared transitive renders one collapsed chain (documented)
# ---------------------------------------------------------------------------


def test_promise_e_shared_transitive_renders_documented_single_collapsed_chain(
    runner, shared_transitive_project: Path
):
    """Promise E: the docs explicitly state the lockfile records "a single
    resolved parent per package" and so ``why`` returns "one root-to-target
    chain (not a fan-out)". This test pins that contract: two declared
    roots that both depend on the same transitive collapse to exactly one
    chain in the output, anchored on the recorded ``resolved_by`` parent."""
    with _cwd(shared_transitive_project):
        result = runner.invoke(cli, ["deps", "why", "shared-utils", "--json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["package"]["repo_url"] == "acme/shared-utils"
    assert payload["package"]["is_direct"] is False
    assert len(payload["paths"]) == 1
    chain = payload["paths"][0]["chain"]
    assert [link["repo_url"] for link in chain] == [
        "acme/big-skills",
        "acme/shared-utils",
    ]
    # Only the recorded parent (acme/big-skills) appears -- the sibling
    # root (acme/other-skills) must NOT be invented by the walker.
    assert "acme/other-skills" not in result.stdout


# ---------------------------------------------------------------------------
# Promise F: is_direct comes from parent_key absence, not chain position
# ---------------------------------------------------------------------------


def test_promise_f_is_direct_reflects_declared_root_not_position(
    runner, truncated_chain_project: Path
):
    """Promise F (Copilot regression-trap, folded into integration tier):
    ``is_direct`` must be derived from ``edge.parent_key is None``, NOT
    from ``idx == 0``. When the recorded parent is missing from the
    lockfile, the walker emits a truncated chain whose first edge still
    has a non-None ``parent_key`` -- that edge must NOT be reported as
    direct in human output (no "declared in apm.yml") or JSON
    (``is_direct: false`` everywhere)."""
    with _cwd(truncated_chain_project):
        human = runner.invoke(cli, ["deps", "why", "orphan"])
        as_json = runner.invoke(cli, ["deps", "why", "orphan", "--json"])
    assert human.exit_code == 0, human.stderr
    assert as_json.exit_code == 0, as_json.stderr

    # Human output: no false "declared in apm.yml" claim.
    assert "declared in apm.yml" not in human.stdout

    # JSON: package itself is not direct AND no edge in the truncated
    # chain claims directness.
    payload = json.loads(as_json.stdout)
    assert payload["package"]["is_direct"] is False
    for path in payload["paths"]:
        for edge in path["chain"]:
            assert edge["is_direct"] is False, (
                f"no edge in a truncated chain may be marked direct; got {edge}"
            )


# ---------------------------------------------------------------------------
# Promise G: lockfile is resolved at the project-root cwd (CLI convention)
# ---------------------------------------------------------------------------


def test_promise_g_resolves_lockfile_at_project_root_cwd(runner, linear_chain_project: Path):
    """Promise G: ``apm deps why`` reads ``apm.lock.yaml`` from the
    current working directory. This pins the project-scope contract
    shared by all ``apm deps`` subcommands: cwd == project root.

    The regression trap has two halves:

    * At the project root the command succeeds with exit 0.
    * From a sibling directory (no lockfile present) the command
      exits 2 with the documented "no apm.lock.yaml" error.
      A future change that silently walked the parent tree -- or
      silently fell back to a different scope -- would flip the
      sibling-dir half of this assertion.
    """
    # Success at the project root.
    with _cwd(linear_chain_project):
        ok = runner.invoke(cli, ["deps", "why", "deep-core"])
    assert ok.exit_code == 0, ok.stderr

    # No upward walk: a sibling directory must NOT inherit the lockfile.
    sibling = linear_chain_project.parent / "sibling"
    sibling.mkdir()
    with _cwd(sibling):
        missing = runner.invoke(cli, ["deps", "why", "deep-core"])
    assert missing.exit_code == 2
    combined = (missing.stdout or "") + (missing.stderr or "")
    assert "no apm.lock.yaml" in combined


# ---------------------------------------------------------------------------
# Promise H: --help text matches actual behavior
# ---------------------------------------------------------------------------


def test_promise_h_help_text_documents_actual_flags_and_argument(runner):
    """Promise H: ``apm deps why --help`` advertises the same surface
    the command actually implements -- the ``PACKAGE`` argument, the
    ``--json`` flag, the ``--global``/``-g`` flag, and at least one
    runnable example. Drift between help text and implementation is a
    silent UX regression."""
    result = runner.invoke(cli, ["deps", "why", "--help"])
    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert "PACKAGE" in out
    # Find the option-listing line that describes the JSON flag and assert
    # it advertises the actual flag name. Anchoring on the help-string
    # ("Emit machine-readable JSON") and the flag spelling on the SAME
    # line defends against a rename that would otherwise hide behind the
    # unchanged example block.
    json_line = next(
        (line for line in out.splitlines() if "Emit machine-readable JSON" in line),
        "",
    )
    assert "--json" in json_line, f"flag spelling drift; line was: {json_line!r}"
    # The --global / -g flag listing is also a contract.
    global_line = next(
        (line for line in out.splitlines() if "user-scope lockfile" in line),
        "",
    )
    assert "--global" in global_line and "-g" in global_line
    assert "apm deps why" in out  # at least one runnable example
