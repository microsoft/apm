"""Deterministic scenarios for shepherd-driver's canonical owner gate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from apm_cli.utils.git_env import get_git_executable, git_subprocess_env

# Entire module: every scenario here drives the gate through the fixed
# _run_git()/_git_executable() path (microsoft/apm#2233's WinError 2
# FileNotFoundError), since the fixture helper below and the gate
# script itself both resolve git via the canonical apm_cli.utils.git_env
# owner. Selected by the PR-time Windows Compatibility Gate via
# `pytest -m windows_compat`; also runs on every other OS.
pytestmark = pytest.mark.windows_compat

ROOT = Path(__file__).parents[2]
GATE = ROOT / "packages/shepherd-driver/scripts/owner_touch_gate.py"
OWNER_TABLE = ".apm/instructions/architecture.instructions.md"
REGISTRY_ROOT = ".apm/architecture/owners"
REGISTRY_INDEX = f"{REGISTRY_ROOT}/index.json"
DECISION = "Fixture durable fact"
OWNER_PATH = "src/apm_cli/fixture_owner.py"


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a fixed, non-interactive test command."""
    return subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def _git(repo: Path, *args: str) -> str:
    """Run git in a fixture repository and return stdout.

    Resolves git's full executable path and uses a sanitized subprocess
    environment via the canonical ``apm_cli.utils.git_env`` owner --
    a bare ``["git", ...]`` argv does not reliably resolve on Windows
    (``CreateProcess`` requires an extension-qualified executable or a
    PATH lookup that ``shell=False`` does not perform), which previously
    raised ``FileNotFoundError: [WinError 2]`` (see microsoft/apm#2233).
    """
    result = subprocess.run(
        [get_git_executable(), *args],
        cwd=repo,
        env=git_subprocess_env(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _table(
    *,
    header: str = "Owner path selectors",
    decision: str = DECISION,
    owner: str = OWNER_PATH,
    selector: str = OWNER_PATH,
    delimiter: str = "---",
) -> str:
    """Return the canonical marked table used by fixture commits."""
    return (
        "# Architecture\n\n"
        "<!-- canonical-owner-table:v1 -->\n"
        f"| Decision / fact | Canonical owner | {header} |\n"
        f"|{delimiter}|{delimiter}|{delimiter}|\n"
        f"| {decision} | `{owner}` | `{selector}` |\n"
        "<!-- /canonical-owner-table -->\n"
    )


def _registry_owner(
    *,
    owner_id: str = "fixture-durable-fact",
    decision: str = DECISION,
    selector: str = OWNER_PATH,
) -> dict[str, Any]:
    """Return one valid version-1 registry owner."""
    return {
        "id": owner_id,
        "decision": decision,
        "owner": f"`{selector}`",
        "selectors": [selector],
        "guards": [f"registry-delegation-{owner_id}"],
    }


def _write_registry(
    repo: Path,
    *,
    owners: list[dict[str, Any]] | None = None,
    shards: list[str] | None = None,
    indent: int | None = 2,
) -> None:
    """Write a fixture registry index and its default listed shard."""
    shard_names = shards or ["core-runtime.json"]
    _write(
        repo,
        REGISTRY_INDEX,
        json.dumps({"version": 1, "shards": shard_names}, indent=indent) + "\n",
    )
    if "core-runtime.json" in shard_names:
        _write(
            repo,
            f"{REGISTRY_ROOT}/core-runtime.json",
            json.dumps(
                {"version": 1, "owners": owners or [_registry_owner()]},
                indent=indent,
            )
            + "\n",
        )


def _write(repo: Path, relative_path: str, content: str) -> None:
    """Write a fixture file, creating its parent directories."""
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="ascii")


def _commit(repo: Path, message: str) -> str:
    """Commit all fixture changes and return the exact commit SHA."""
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def owner_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a git repository whose base contains one canonical owner."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "Fixture")
    _write(repo, OWNER_TABLE, _table())
    _write(repo, OWNER_PATH, 'FACT = "base"\n')
    return repo, _commit(repo, "base")


def _gate(
    repo: Path,
    command: str,
    base: str,
    head: str,
    *,
    completion: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the owner gate exactly as the shepherd primitive does."""
    args = [
        sys.executable,
        str(GATE),
        command,
        "--repo-root",
        str(repo),
        "--base",
        base,
        "--head",
        head,
    ]
    if completion is not None:
        args.extend(["--completion", str(completion)])
    return _run(args, cwd=repo)


def _detect(repo: Path, base: str, head: str) -> dict[str, Any]:
    """Run detection and decode its JSON result."""
    result = _gate(repo, "detect", base, head)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _completion(
    report: dict[str, Any],
    *,
    classification: str,
    include_test: bool,
) -> dict[str, Any]:
    """Build terminal evidence for semantic gate scenarios."""
    tests = []
    if include_test:
        tests.append(
            {
                "test_id": "tests/fixture.py::test_fact",
                "command": "pytest tests/fixture.py::test_fact -q",
                "outcome": "passed",
                "head_sha": report["head_sha"],
                "owner_decisions": [DECISION],
                "run_evidence": "1 passed in 0.01s",
            }
        )
    return {
        "status": "ready-to-merge",
        "architecture_evidence": {
            "version": "2",
            "classification": classification,
            "owner_touch_report": report,
            "functional_tests": tests,
        },
    }


def _verify(
    repo: Path,
    base: str,
    head: str,
    completion: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    """Persist and semantically verify one completion fixture."""
    completion_path = repo / "completion.json"
    completion_path.write_text(json.dumps(completion), encoding="ascii")
    return _gate(repo, "verify", base, head, completion=completion_path)


def test_positive_owner_touch_is_deterministic_and_verifies(
    owner_repo: tuple[Path, str],
) -> None:
    """A touched owner with passing exact-head evidence verifies."""
    repo, base = owner_repo
    _write(repo, OWNER_PATH, 'FACT = "changed"\n')
    head = _commit(repo, "touch owner")

    first = _detect(repo, base, head)
    second = _detect(repo, base, head)
    result = _verify(
        repo,
        base,
        head,
        _completion(first, classification="owner-extension", include_test=True),
    )

    assert first == second
    assert first["touched_owners"][0]["decision"] == DECISION
    assert first["touched_owners"][0]["matched_files"] == [OWNER_PATH]
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["functional_test_ids"] == ["tests/fixture.py::test_fact"]


def test_missing_functional_evidence_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """A touched owner without an executed test cannot verify."""
    repo, base = owner_repo
    _write(repo, OWNER_PATH, 'FACT = "changed"\n')
    head = _commit(repo, "touch owner")
    report = _detect(repo, base, head)

    result = _verify(
        repo,
        base,
        head,
        _completion(report, classification="owner-extension", include_test=False),
    )

    assert result.returncode == 1
    assert "missing executed functional evidence" in result.stderr


def test_false_self_classification_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """An LLM cannot label a detected owner touch ordinary."""
    repo, base = owner_repo
    _write(repo, OWNER_PATH, 'FACT = "changed"\n')
    head = _commit(repo, "touch owner")
    report = _detect(repo, base, head)

    result = _verify(
        repo,
        base,
        head,
        _completion(report, classification="ordinary-fix", include_test=True),
    )

    assert result.returncode == 1
    assert "classification self-exempts" in result.stderr


def test_unrelated_diff_has_no_owner_touch_and_needs_no_test(
    owner_repo: tuple[Path, str],
) -> None:
    """An unrelated primitive diff does not create a functional-test burden."""
    repo, base = owner_repo
    _write(repo, ".apm/skills/fixture/SKILL.md", "# Fixture\n")
    head = _commit(repo, "unrelated primitive")
    report = _detect(repo, base, head)

    result = _verify(
        repo,
        base,
        head,
        _completion(report, classification="not-applicable", include_test=False),
    )

    assert report["touched_owners"] == []
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["touched_owner_count"] == 0


def test_owner_table_header_drift_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """A changed owner-table contract cannot silently disable detection."""
    repo, base = owner_repo
    _write(repo, OWNER_TABLE, _table(header="Selectors"))
    head = _commit(repo, "drift owner table")

    result = _gate(repo, "detect", base, head)

    assert result.returncode == 1
    assert "canonical owner table header drifted" in result.stderr


@pytest.mark.parametrize(
    ("table", "diagnostic"),
    [
        (_table(delimiter=":"), "canonical owner table delimiter drifted"),
        (
            _table(selector=f"{OWNER_PATH};;src/apm_cli/other.py"),
            "canonical owner row has no selectors",
        ),
        (
            _table(selector=f"`{OWNER_PATH}"),
            "malformed owner path selector",
        ),
    ],
)
def test_malformed_owner_table_syntax_fails_closed(
    owner_repo: tuple[Path, str],
    table: str,
    diagnostic: str,
) -> None:
    """Malformed delimiters and selector segments are never normalized."""
    repo, base = owner_repo
    _write(repo, OWNER_TABLE, table)
    head = _commit(repo, "malform owner table")

    result = _gate(repo, "detect", base, head)

    assert result.returncode == 1
    assert diagnostic in result.stderr


def test_owner_table_unmatchable_selector_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """A stale selector cannot silently make its owner row unreachable."""
    repo, base = owner_repo
    _write(repo, OWNER_TABLE, _table(selector="src/apm_cli/missing.py"))
    head = _commit(repo, "drift owner selector")

    result = _gate(repo, "detect", base, head)

    assert result.returncode == 1
    assert "canonical owner selector matches no exact-head file" in result.stderr


def test_stale_owner_table_report_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """A report captured before owner-table drift cannot verify a later head."""
    repo, base = owner_repo
    _write(repo, OWNER_PATH, 'FACT = "changed"\n')
    first_head = _commit(repo, "touch owner")
    stale_report = _detect(repo, base, first_head)
    _write(repo, OWNER_TABLE, _table() + "\n# Clarified owner notes\n")
    current_head = _commit(repo, "clarify owner table")

    result = _verify(
        repo,
        base,
        current_head,
        _completion(
            stale_report,
            classification="owner-extension",
            include_test=True,
        ),
    )

    assert result.returncode == 1
    assert "owner_touch_report does not match fresh exact-head detection" in result.stderr


def test_deleted_owner_under_broad_selector_is_detected(
    owner_repo: tuple[Path, str],
) -> None:
    """Deleted owner paths remain part of deterministic touch detection."""
    repo, _ = owner_repo
    _write(repo, OWNER_TABLE, _table(selector="src/apm_cli/*.py"))
    _write(repo, "src/apm_cli/other.py", 'FACT = "other"\n')
    base = _commit(repo, "broaden owner selector")
    (repo / OWNER_PATH).unlink()
    head = _commit(repo, "delete owner")

    report = _detect(repo, base, head)

    assert report["touched_owners"][0]["matched_files"] == [OWNER_PATH]


def test_type_changed_owner_is_detected(owner_repo: tuple[Path, str]) -> None:
    """Replacing an owner file with a symlink cannot bypass the gate."""
    repo, base = owner_repo
    owner = repo / OWNER_PATH
    owner.unlink()
    owner.symlink_to("fixture_target.py")
    _write(repo, "src/apm_cli/fixture_target.py", 'FACT = "target"\n')
    head = _commit(repo, "replace owner with symlink")

    report = _detect(repo, base, head)

    assert report["touched_owners"][0]["matched_files"] == [OWNER_PATH]


def test_rename_away_keeps_old_owner_endpoint(
    owner_repo: tuple[Path, str],
) -> None:
    """A broad selector detects an owner renamed outside its matched tree."""
    repo, _ = owner_repo
    _write(repo, OWNER_TABLE, _table(selector="src/apm_cli/*.py"))
    _write(repo, "src/apm_cli/other.py", 'FACT = "other"\n')
    base = _commit(repo, "broaden owner selector")
    moved = repo / "archive/fixture_owner.py"
    moved.parent.mkdir()
    (repo / OWNER_PATH).rename(moved)
    head = _commit(repo, "rename owner away")

    report = _detect(repo, base, head)

    assert report["touched_owners"][0]["matched_files"] == [OWNER_PATH]


def test_copy_away_keeps_unchanged_owner_source_endpoint(
    owner_repo: tuple[Path, str],
) -> None:
    """Copy detection includes an unchanged owner source outside the destination tree."""
    repo, _ = owner_repo
    _write(repo, OWNER_TABLE, _table(selector="src/apm_cli/*.py"))
    base = _commit(repo, "broaden owner selector")
    copied = repo / "archive/fixture_owner.py"
    copied.parent.mkdir()
    copied.write_bytes((repo / OWNER_PATH).read_bytes())
    head = _commit(repo, "copy owner away")

    report = _detect(repo, base, head)

    assert report["changed_files"] == ["archive/fixture_owner.py", OWNER_PATH]
    assert report["touched_owners"][0]["matched_files"] == [OWNER_PATH]


def test_copy_into_owner_scope_preserves_both_copy_endpoints(
    owner_repo: tuple[Path, str],
) -> None:
    """A copied destination is matched while the unchanged source stays in the diff."""
    repo, _ = owner_repo
    source_path = "archive/source.py"
    destination_path = "src/apm_cli/copied.py"
    _write(repo, OWNER_TABLE, _table(selector="src/apm_cli/*.py"))
    _write(repo, source_path, 'FACT = "copy source"\n')
    base = _commit(repo, "add copy source")
    (repo / destination_path).write_bytes((repo / source_path).read_bytes())
    head = _commit(repo, "copy into owner scope")

    report = _detect(repo, base, head)

    assert report["changed_files"] == [source_path, destination_path]
    assert report["touched_owners"][0]["matched_files"] == [destination_path]


def test_removed_owner_row_still_detects_base_selector(
    owner_repo: tuple[Path, str],
) -> None:
    """Removing an owner row and its file cannot erase the prior authority."""
    repo, _ = owner_repo
    second_row = "| Other fact | `src/apm_cli/other.py` | `src/apm_cli/other.py` |\n"
    table = _table().replace(
        "<!-- /canonical-owner-table -->",
        second_row + "<!-- /canonical-owner-table -->",
    )
    _write(repo, OWNER_TABLE, table)
    _write(repo, "src/apm_cli/other.py", 'FACT = "other"\n')
    base = _commit(repo, "add second owner")
    table_without_owner = table.replace(
        f"| {DECISION} | `{OWNER_PATH}` | `{OWNER_PATH}` |\n",
        "",
    )
    _write(repo, OWNER_TABLE, table_without_owner)
    (repo / OWNER_PATH).unlink()
    head = _commit(repo, "remove owner row and file")

    report = _detect(repo, base, head)

    assert report["touched_owners"][0]["decision"] == DECISION
    assert report["touched_owners"][0]["matched_files"] == [OWNER_TABLE, OWNER_PATH]


def test_legacy_owner_row_removal_alone_touches_prior_owner_source(
    owner_repo: tuple[Path, str],
) -> None:
    """Removing only legacy metadata attributes the touch to the Markdown table."""
    repo, _ = owner_repo
    other_path = "src/apm_cli/other.py"
    second_row = f"| Other fact | `{other_path}` | `{other_path}` |\n"
    table = _table().replace(
        "<!-- /canonical-owner-table -->",
        second_row + "<!-- /canonical-owner-table -->",
    )
    _write(repo, OWNER_TABLE, table)
    _write(repo, other_path, 'FACT = "other"\n')
    base = _commit(repo, "add second legacy owner")
    _write(
        repo,
        OWNER_TABLE,
        table.replace(f"| {DECISION} | `{OWNER_PATH}` | `{OWNER_PATH}` |\n", ""),
    )
    head = _commit(repo, "remove legacy owner metadata only")

    report = _detect(repo, base, head)
    removed = next(item for item in report["touched_owners"] if item["decision"] == DECISION)

    assert removed["matched_files"] == [OWNER_TABLE]


@pytest.mark.parametrize("field", ["decision", "owner", "selector"])
def test_legacy_owner_metadata_change_alone_touches_exact_table_source(
    owner_repo: tuple[Path, str],
    field: str,
) -> None:
    """Rewording or reassigning a legacy row cannot evade owner-touch evidence."""
    repo, base = owner_repo
    new_decision = "Reworded fixture durable fact"
    new_owner = "src/apm_cli/reassigned_owner.py"
    new_selector = "src/apm_cli/reassigned_selector.py"
    if field == "selector":
        _write(repo, new_selector, 'FACT = "selector target"\n')
        base = _commit(repo, "add future selector target")

    _write(
        repo,
        OWNER_TABLE,
        _table(
            decision=new_decision if field == "decision" else DECISION,
            owner=new_owner if field == "owner" else OWNER_PATH,
            selector=new_selector if field == "selector" else OWNER_PATH,
        ),
    )
    head = _commit(repo, f"change legacy owner {field}")

    report = _detect(repo, base, head)

    assert report["touched_owners"]
    assert all(item["matched_files"] == [OWNER_TABLE] for item in report["touched_owners"])
    if field == "decision":
        assert {item["decision"] for item in report["touched_owners"]} == {
            DECISION,
            new_decision,
        }
    else:
        assert len(report["touched_owners"]) == 1
        touched = report["touched_owners"][0]
        assert touched["owner"] == (f"`{new_owner}`" if field == "owner" else f"`{OWNER_PATH}`")
        if field == "selector":
            assert touched["selectors"] == sorted([OWNER_PATH, new_selector])


def test_legacy_base_and_json_head_use_exact_revision_sources(
    owner_repo: tuple[Path, str],
) -> None:
    """A registry introduction upgrades only the head ownership source."""
    repo, base = owner_repo
    _write_registry(repo)
    _write(repo, OWNER_PATH, 'FACT = "json head"\n')
    head = _commit(repo, "introduce registry")

    report = _detect(repo, base, head)

    assert set(report) == {
        "version",
        "owner_table",
        "owner_table_sha256",
        "base_sha",
        "head_sha",
        "changed_files",
        "touched_owners",
    }
    assert report["version"] == "1"
    assert report["owner_table"] == OWNER_TABLE
    assert report["touched_owners"][0]["decision"] == DECISION
    assert len(report["owner_table_sha256"]) == 64


def test_json_base_and_json_head_detect_owner_touch(
    owner_repo: tuple[Path, str],
) -> None:
    """Both sides validate and use their own exact-revision JSON registries."""
    repo, _ = owner_repo
    _write_registry(repo)
    base = _commit(repo, "registry base")
    _write(repo, OWNER_PATH, 'FACT = "json changed"\n')
    head = _commit(repo, "touch JSON owner")

    report = _detect(repo, base, head)

    assert report["touched_owners"][0]["matched_files"] == [OWNER_PATH]


def test_json_formatting_only_preserves_semantic_hash(
    owner_repo: tuple[Path, str],
) -> None:
    """Whitespace and key formatting cannot stale completion evidence."""
    repo, _ = owner_repo
    _write_registry(repo, indent=2)
    base = _commit(repo, "formatted registry")
    before = _detect(repo, base, base)
    _write_registry(repo, indent=None)
    head = _commit(repo, "compact registry")
    after = _detect(repo, base, head)

    assert before["owner_table_sha256"] == after["owner_table_sha256"]
    assert after["touched_owners"] == []


def test_json_array_reordering_preserves_semantics(
    owner_repo: tuple[Path, str],
) -> None:
    """Selector and guard order cannot create a false owner touch."""
    repo, _ = owner_repo
    other_path = "src/apm_cli/other.py"
    _write(repo, other_path, 'FACT = "other"\n')
    owner = _registry_owner()
    owner["selectors"] = [OWNER_PATH, other_path]
    owner["guards"] = [
        "registry-delegation-fixture-durable-fact",
        "registry-delegation-fixture-secondary",
    ]
    _write_registry(repo, owners=[owner])
    base = _commit(repo, "ordered registry arrays")
    before = _detect(repo, base, base)
    owner["selectors"].reverse()
    owner["guards"].reverse()
    _write_registry(repo, owners=[owner])
    head = _commit(repo, "reorder registry arrays")
    after = _detect(repo, base, head)

    assert after["owner_table_sha256"] == before["owner_table_sha256"]
    assert after["touched_owners"] == []


def test_json_base_and_removed_registry_head_fail_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """A head cannot downgrade to legacy Markdown after adopting JSON."""
    repo, _ = owner_repo
    _write_registry(repo)
    base = _commit(repo, "registry base")
    (repo / REGISTRY_INDEX).unlink()
    (repo / f"{REGISTRY_ROOT}/core-runtime.json").unlink()
    head = _commit(repo, "remove registry")

    result = _gate(repo, "detect", base, head)

    assert result.returncode == 1
    assert "removed the canonical owner registry" in result.stderr


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        ("malformed", "malformed JSON"),
        ("missing", "missing registry shards"),
        ("unlisted", "unlisted registry shards"),
    ],
)
def test_invalid_registry_shape_fails_closed(
    owner_repo: tuple[Path, str],
    mutation: str,
    diagnostic: str,
) -> None:
    """Malformed, missing, and unlisted exact-revision shards never fall back."""
    repo, base = owner_repo
    if mutation == "malformed":
        _write(repo, REGISTRY_INDEX, "{not-json\n")
    elif mutation == "missing":
        _write_registry(repo, shards=["missing.json"])
    else:
        _write_registry(repo)
        _write(
            repo,
            f"{REGISTRY_ROOT}/unlisted.json",
            json.dumps({"version": 1, "owners": [_registry_owner()]}) + "\n",
        )
    head = _commit(repo, f"{mutation} registry")

    result = _gate(repo, "detect", base, head)

    assert result.returncode == 1
    assert diagnostic in result.stderr


def test_json_selector_mismatch_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """Every JSON selector must match the head's own tracked inventory."""
    repo, base = owner_repo
    _write_registry(
        repo,
        owners=[_registry_owner(selector="src/apm_cli/missing.py")],
    )
    head = _commit(repo, "stale JSON selector")

    result = _gate(repo, "detect", base, head)

    assert result.returncode == 1
    assert "registry selector matches no exact-revision file" in result.stderr


def test_removed_json_owner_still_detects_base_selector(
    owner_repo: tuple[Path, str],
) -> None:
    """Removing a JSON owner and its file retains the base authority."""
    repo, _ = owner_repo
    other_path = "src/apm_cli/other.py"
    _write(repo, other_path, 'FACT = "other"\n')
    _write_registry(
        repo,
        owners=[
            _registry_owner(),
            _registry_owner(
                owner_id="other-fact",
                decision="Other fact",
                selector=other_path,
            ),
        ],
    )
    base = _commit(repo, "two JSON owners")
    _write_registry(
        repo,
        owners=[
            _registry_owner(
                owner_id="other-fact",
                decision="Other fact",
                selector=other_path,
            )
        ],
    )
    (repo / OWNER_PATH).unlink()
    head = _commit(repo, "remove JSON owner")

    report = _detect(repo, base, head)
    removed = next(item for item in report["touched_owners"] if item["decision"] == DECISION)

    assert removed["matched_files"] == [
        f"{REGISTRY_ROOT}/core-runtime.json",
        OWNER_PATH,
    ]


def test_removed_json_owner_without_source_deletion_requires_evidence(
    owner_repo: tuple[Path, str],
) -> None:
    """Removing registry metadata alone still touches the removed owner."""
    repo, _ = owner_repo
    other_path = "src/apm_cli/other.py"
    _write(repo, other_path, 'FACT = "other"\n')
    other_owner = _registry_owner(
        owner_id="other-fact",
        decision="Other fact",
        selector=other_path,
    )
    _write_registry(repo, owners=[_registry_owner(), other_owner])
    base = _commit(repo, "two registry owners")
    _write_registry(repo, owners=[other_owner])
    head = _commit(repo, "remove owner metadata")

    report = _detect(repo, base, head)
    removed = next(item for item in report["touched_owners"] if item["decision"] == DECISION)

    assert removed["matched_files"] == [f"{REGISTRY_ROOT}/core-runtime.json"]


def test_selector_reassignment_requires_evidence_for_both_owners(
    owner_repo: tuple[Path, str],
) -> None:
    """Moving selectors between stable IDs touches both durable owners."""
    repo, _ = owner_repo
    other_path = "src/apm_cli/other.py"
    other_decision = "Other fact"
    _write(repo, other_path, 'FACT = "other"\n')
    _write_registry(
        repo,
        owners=[
            _registry_owner(),
            _registry_owner(
                owner_id="other-fact",
                decision=other_decision,
                selector=other_path,
            ),
        ],
    )
    base = _commit(repo, "assign registry selectors")
    _write_registry(
        repo,
        owners=[
            _registry_owner(selector=other_path),
            _registry_owner(
                owner_id="other-fact",
                decision=other_decision,
                selector=OWNER_PATH,
            ),
        ],
    )
    head = _commit(repo, "reassign registry selectors")

    report = _detect(repo, base, head)
    touched = {item["decision"]: item for item in report["touched_owners"]}

    assert set(touched) == {DECISION, other_decision}
    assert touched[DECISION]["matched_files"] == [f"{REGISTRY_ROOT}/core-runtime.json"]
    assert touched[other_decision]["matched_files"] == [f"{REGISTRY_ROOT}/core-runtime.json"]


def test_stable_owner_id_change_requires_evidence(
    owner_repo: tuple[Path, str],
) -> None:
    """Replacing a stable ID cannot look like a formatting-only registry edit."""
    repo, _ = owner_repo
    _write_registry(repo)
    base = _commit(repo, "stable registry ID")
    _write_registry(
        repo,
        owners=[_registry_owner(owner_id="replacement-durable-fact")],
    )
    head = _commit(repo, "replace stable registry ID")

    report = _detect(repo, base, head)

    assert len(report["touched_owners"]) == 1
    assert report["touched_owners"][0]["decision"] == DECISION
    assert report["touched_owners"][0]["matched_files"] == [f"{REGISTRY_ROOT}/core-runtime.json"]


@pytest.mark.parametrize(
    ("field", "replacement", "expected_decision"),
    [
        ("decision", "Reworded fixture durable fact", "Reworded fixture durable fact"),
        ("owner", "`src/apm_cli/reassigned_owner.py`", DECISION),
        ("guards", ["registry-delegation-replacement-guard"], DECISION),
    ],
)
def test_registry_only_metadata_change_requires_evidence(
    owner_repo: tuple[Path, str],
    field: str,
    replacement: Any,
    expected_decision: str,
) -> None:
    """Mutable registry metadata cannot change without touching its stable owner."""
    repo, _ = owner_repo
    _write_registry(repo)
    base = _commit(repo, "registry metadata base")
    updated_owner = _registry_owner()
    updated_owner[field] = replacement
    _write_registry(repo, owners=[updated_owner])
    head = _commit(repo, f"change owner {field}")

    report = _detect(repo, base, head)

    assert report["touched_owners"] == [
        {
            "decision": expected_decision,
            "owner": updated_owner["owner"],
            "selectors": [OWNER_PATH],
            "matched_files": [f"{REGISTRY_ROOT}/core-runtime.json"],
        }
    ]


def test_json_metadata_changes_attribute_each_owner_to_its_exact_shard(
    owner_repo: tuple[Path, str],
) -> None:
    """Simultaneous shard edits do not smear every registry path onto every owner."""
    repo, _ = owner_repo
    other_path = "src/apm_cli/other.py"
    other_owner = _registry_owner(
        owner_id="other-fact",
        decision="Other fact",
        selector=other_path,
    )
    _write(repo, other_path, 'FACT = "other"\n')
    _write_registry(
        repo,
        owners=[_registry_owner()],
        shards=["core-runtime.json", "contracts-tooling.json"],
    )
    _write(
        repo,
        f"{REGISTRY_ROOT}/contracts-tooling.json",
        json.dumps({"version": 1, "owners": [other_owner]}, indent=2) + "\n",
    )
    base = _commit(repo, "split owners across registry shards")

    current_owner = _registry_owner(decision="Changed fixture fact")
    changed_other = _registry_owner(
        owner_id="other-fact",
        decision="Changed other fact",
        selector=other_path,
    )
    _write(
        repo,
        f"{REGISTRY_ROOT}/core-runtime.json",
        json.dumps({"version": 1, "owners": [current_owner]}, indent=2) + "\n",
    )
    _write(
        repo,
        f"{REGISTRY_ROOT}/contracts-tooling.json",
        json.dumps({"version": 1, "owners": [changed_other]}, indent=2) + "\n",
    )
    head = _commit(repo, "change metadata in both owner shards")

    report = _detect(repo, base, head)
    touched = {item["decision"]: item for item in report["touched_owners"]}

    assert touched["Changed fixture fact"]["matched_files"] == [
        f"{REGISTRY_ROOT}/core-runtime.json"
    ]
    assert touched["Changed other fact"]["matched_files"] == [
        f"{REGISTRY_ROOT}/contracts-tooling.json"
    ]


def test_partial_registry_without_index_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """A tracked owner shard cannot silently trigger legacy fallback."""
    repo, base = owner_repo
    _write(
        repo,
        f"{REGISTRY_ROOT}/core-runtime.json",
        json.dumps({"version": 1, "owners": [_registry_owner()]}) + "\n",
    )
    head = _commit(repo, "partial owner registry")

    result = _gate(repo, "detect", base, head)

    assert result.returncode == 1
    assert "registry artifacts exist without" in result.stderr


def test_unlisted_case_variant_json_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """Unexpected JSON-like files are detected case-insensitively."""
    repo, base = owner_repo
    _write_registry(repo)
    _write(
        repo,
        f"{REGISTRY_ROOT}/hidden.JSON",
        json.dumps({"version": 1, "owners": [_registry_owner()]}) + "\n",
    )
    head = _commit(repo, "add uppercase unlisted registry file")

    result = _gate(repo, "detect", base, head)

    assert result.returncode == 1
    assert "unlisted registry shards" in result.stderr


def test_duplicate_guard_id_across_shards_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """The standalone parser also enforces one owner per guard ID."""
    repo, base = owner_repo
    other_path = "src/apm_cli/other.py"
    _write(repo, other_path, 'FACT = "other"\n')
    _write_registry(repo, shards=["core-runtime.json", "contracts-tooling.json"])
    other_owner = _registry_owner(
        owner_id="other-fact",
        decision="Other fact",
        selector=other_path,
    )
    other_owner["guards"] = ["registry-delegation-fixture-durable-fact"]
    _write(
        repo,
        f"{REGISTRY_ROOT}/contracts-tooling.json",
        json.dumps({"version": 1, "owners": [other_owner]}) + "\n",
    )
    head = _commit(repo, "duplicate owner guard")

    result = _gate(repo, "detect", base, head)

    assert result.returncode == 1
    assert "guard ID assigned to multiple owners" in result.stderr


def test_cross_shard_selector_overlap_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """A tracked file cannot match selectors from owners in separate shards."""
    repo, base = owner_repo
    other_path = "src/apm_cli/other.py"
    _write(repo, other_path, 'FACT = "other"\n')
    _write_registry(
        repo,
        owners=[_registry_owner(selector="src/apm_cli/*.py")],
        shards=["core-runtime.json", "contracts-tooling.json"],
    )
    _write(
        repo,
        f"{REGISTRY_ROOT}/contracts-tooling.json",
        json.dumps(
            {
                "version": 1,
                "owners": [
                    _registry_owner(
                        owner_id="other-fact",
                        decision="Other fact",
                        selector=other_path,
                    )
                ],
            }
        )
        + "\n",
    )
    head = _commit(repo, "overlap owner selectors")

    result = _gate(repo, "detect", base, head)

    assert result.returncode == 1
    assert "matches selectors from multiple owners" in result.stderr


def test_unknown_completion_status_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """Only schema-defined non-terminal statuses bypass evidence checks."""
    repo, base = owner_repo
    _write(repo, ".apm/skills/fixture/SKILL.md", "# Fixture\n")
    head = _commit(repo, "unrelated primitive")

    result = _verify(repo, base, head, {"status": "ready"})

    assert result.returncode == 1
    assert "unsupported completion status" in result.stderr


# ---------------------------------------------------------------------------
# Cross-row duplicate selector regression (architecture integrity gate)
#
# The canonical owner table must have a unique selector per row; a selector
# that appears in two DISTINCT rows is an ambiguous ownership claim that the
# gate must reject fail-closed.  This test constructs that exact scenario
# -- two rows with different decisions but the same path selector -- and
# asserts that the gate emits the exact diagnostic text
# "duplicate canonical owner selector".
# ---------------------------------------------------------------------------


def _table_with_cross_row_dup(selector: str = OWNER_PATH) -> str:
    """Return a canonical table with TWO distinct rows sharing one selector."""
    return (
        "# Architecture\n\n"
        "<!-- canonical-owner-table:v1 -->\n"
        "| Decision / fact | Canonical owner | Owner path selectors |\n"
        "|---|---|---|\n"
        f"| First distinct decision | `owner-a` | `{selector}` |\n"
        f"| Second distinct decision | `owner-b` | `{selector}` |\n"
        "<!-- /canonical-owner-table -->\n"
    )


def test_cross_row_duplicate_selector_fails_closed(
    owner_repo: tuple[Path, str],
) -> None:
    """Two distinct rows sharing a selector MUST be rejected fail-closed.

    Regression for the architecture instruction integrity rule: every row
    in the canonical owner table must carry a unique set of path selectors.
    A selector shared across two different decision rows is ambiguous
    (neither row is the unambiguous canonical owner) and MUST produce
    fail-closed output containing 'duplicate canonical owner selector'.
    """
    repo, base = owner_repo

    # Overwrite the owner table with two rows that share OWNER_PATH.
    _write(repo, OWNER_TABLE, _table_with_cross_row_dup(OWNER_PATH))
    _write(repo, OWNER_PATH, 'FACT = "dup"\n')
    head = _commit(repo, "introduce cross-row duplicate selector")

    result = _gate(repo, "detect", base, head)

    assert result.returncode != 0, "detect must fail closed when two rows share a selector"
    assert "duplicate canonical owner selector" in result.stderr, (
        f"Expected 'duplicate canonical owner selector' in stderr, got: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Windows bare-argv git resolution regression (microsoft/apm#2233)
#
# subprocess.run(["git", ...]) does not reliably resolve on Windows with
# shell=False (CreateProcess needs an extension-qualified path or a PATH
# search this test's environment previously did not perform), raising
# FileNotFoundError: [WinError 2]. The fix resolves git's full path once
# via shutil.which and reuses it for every invocation. These regression
# tests cover the gate script itself (packages/shepherd-driver/scripts/
# owner_touch_gate.py::_git), not just this test file's own fixture
# helper, since both had the identical bug.
# ---------------------------------------------------------------------------


def _load_gate_module() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("owner_touch_gate", GATE)
    assert spec is not None and spec.loader is not None
    module = ModuleType("owner_touch_gate")
    import sys as _sys

    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_gate_git_helper_never_uses_bare_argv() -> None:
    """The production gate script must not pass a bare "git" argv to
    subprocess -- it must resolve git's full path first (see
    microsoft/apm#2233)."""
    source = GATE.read_text(encoding="utf-8")
    assert '["git"' not in source
    assert "['git'" not in source


def test_replacement_refs_cannot_substitute_exact_revision_content(
    owner_repo: tuple[Path, str],
) -> None:
    """A local replacement object cannot hide a changed owner behind the original SHA."""
    repo, base = owner_repo
    _write(repo, OWNER_PATH, 'FACT = "head"\n')
    head = _commit(repo, "change owner")
    _git(repo, "replace", base, head)

    report = _detect(repo, base, head)

    assert report["base_sha"] == base
    assert report["head_sha"] == head
    assert report["changed_files"] == [OWNER_PATH]
    assert report["touched_owners"][0]["matched_files"] == [OWNER_PATH]


def test_gate_git_executable_is_cached_and_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolved git path is looked up via shutil.which and cached."""
    module = _load_gate_module()
    calls = {"count": 0}

    def fake_which(name: str) -> str:
        assert name == "git"
        calls["count"] += 1
        return "/usr/bin/git"

    monkeypatch.setattr(module.shutil, "which", fake_which)
    module._GIT_EXECUTABLE = None

    first = module._git_executable()
    second = module._git_executable()

    assert first == "/usr/bin/git"
    assert second == "/usr/bin/git"
    assert calls["count"] == 1, "git executable lookup must be cached, not repeated"


def test_gate_git_executable_fails_closed_when_git_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing git binary must raise a clear GateError, not an opaque
    platform-specific FileNotFoundError."""
    module = _load_gate_module()
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    module._GIT_EXECUTABLE = None

    with pytest.raises(module.GateError, match="git executable not found"):
        module._git_executable()
