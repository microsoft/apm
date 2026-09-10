"""Strict source diagnostics and read-only planning regression traps."""

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apm_cli.contracts.frontend import parse_contract, plan_contract
from apm_cli.contracts.models import ContractError, ContractLimits, Outcome

pytestmark = pytest.mark.component


def source(tmp_path: Path, header: str = "produces: out.json\nverify:\n  valid: 'true'") -> Path:
    path = tmp_path / "work.contract.md"
    path.write_text("---\n" + header + "\n---\nWrite a result.\n", encoding="utf-8")
    return path


def changed_stat(result: os.stat_result, **changes: int) -> SimpleNamespace:
    """Return a complete stat-like object with selected fields changed."""
    values = {name: getattr(result, name) for name in dir(result) if name.startswith("st_")}
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "apm.yml").write_text("name: fixture\nversion: 1.0.0\n", encoding="utf-8")
    binary = tmp_path / "native"
    binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
    monkeypatch.setattr("apm_cli.contracts.frontend.sys.platform", "linux")
    monkeypatch.setattr("apm_cli.runtime.utils.find_runtime_binary", lambda name: str(binary))
    return tmp_path


@pytest.mark.windows_compat
def test_exact_bom_crlf_body_digest_and_declaration_locations(tmp_path: Path) -> None:
    path = tmp_path / "work.contract.md"
    raw = b"\xef\xbb\xbf---\r\nneeds: input.txt\r\nproduces: out.json\r\nverify:\r\n  valid: 'true'\r\n---\r\n  Body\r\n\r\n"
    path.write_bytes(raw)
    parsed = parse_contract(path)
    assert parsed.body == "  Body\r\n\r\n"
    assert parsed.source_digest == hashlib.sha256(raw).hexdigest()
    assert parsed.needs == ("input.txt",)
    assert parsed.locations["produces"].line == 3
    assert parsed.checks[0].location.line == 5
    assert parsed.checks[0].location.column == 3


@pytest.mark.windows_compat
def test_path_timestamp_drift_does_not_change_source_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = source(tmp_path)
    real_stat = Path.stat

    def stat_with_timestamp_drift(
        candidate: Path, *args, **kwargs
    ) -> os.stat_result | SimpleNamespace:
        result = real_stat(candidate, *args, **kwargs)
        if candidate != path:
            return result
        return changed_stat(
            result,
            st_mtime_ns=result.st_mtime_ns + 1,
            st_ctime_ns=result.st_ctime_ns + 1,
        )

    monkeypatch.setattr(Path, "stat", stat_with_timestamp_drift)

    assert parse_contract(path).produces == "out.json"


@pytest.mark.windows_compat
def test_path_replacement_still_changes_source_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = source(tmp_path)
    real_stat = Path.stat

    def stat_with_replaced_identity(
        candidate: Path, *args, **kwargs
    ) -> os.stat_result | SimpleNamespace:
        result = real_stat(candidate, *args, **kwargs)
        if candidate != path:
            return result
        return changed_stat(
            result,
            st_ino=result.st_ino + 1,
        )

    monkeypatch.setattr(Path, "stat", stat_with_replaced_identity)

    with pytest.raises(ContractError) as error:
        parse_contract(path)
    assert error.value.code == "source_changed"


@pytest.mark.parametrize(
    "header",
    [
        "produces: [one, two]\nverify: {ok: true}",
        "produces: out\nverify: {}",
        "produces: out\nverify: {ok: {run: check}}",
        "produces: out\nverify: {ok: ''}",
        "produces: out\nverify: {true: 'true'}",
        "produces: out\nverify: {ok: 'true'}\nunknown: 1",
        "produces: out\nproduces: other\nverify: {ok: 'true'}",
        "produces: out\nverify:\n  ok: 'true'\n  ok: 'false'",
        "produces: &out out\nverify: {ok: 'true'}",
        "produces: *out\nverify: {ok: 'true'}",
        "produces: !!str out\nverify: {ok: 'true'}",
        "produces: out\nverify: {<<: {}, ok: 'true'}",
        "produces: out\nneeds: {from: input}\nverify: {ok: 'true'}",
        "produces: ../out\nverify: {ok: 'true'}",
        "produces: /tmp/out\nverify: {ok: 'true'}",
        "produces: '${capture}.txt'\nverify: {ok: 'true'}",
        "produces: '*.txt'\nverify: {ok: 'true'}",
        "produces: 'one|two'\nverify: {ok: 'true'}",
        "produces: out\nimports: [apm_modules/_local/pkg]\nverify: {ok: 'true'}",
        "produces: out\nimports: [owner/pkg#v1]\nverify: {ok: 'true'}",
        "produces: out\nimports: [a, b]\nverify: {ok: 'true'}",
    ],
)
def test_invalid_subset_is_source_located(tmp_path: Path, header: str) -> None:
    with pytest.raises(ContractError) as error:
        parse_contract(source(tmp_path, header))
    assert error.value.outcome == Outcome.HALTED
    assert error.value.location is not None
    assert error.value.location.line >= 1


def test_duplicate_nested_key_reports_second_declaration(tmp_path: Path) -> None:
    with pytest.raises(ContractError) as error:
        parse_contract(source(tmp_path, "produces: out\nverify:\n  ok: 'true'\n  ok: 'false'"))
    assert error.value.code == "duplicate_key"
    assert error.value.location.line == 5


@pytest.mark.parametrize("field", ["run: echo hi", "budget: {usd: 1}", "sandbox: {network: none}"])
def test_unsupported_controls_are_unproven(tmp_path: Path, field: str) -> None:
    with pytest.raises(ContractError) as error:
        parse_contract(source(tmp_path, f"produces: out\nverify: {{ok: 'true'}}\n{field}"))
    assert error.value.outcome == Outcome.UNPROVEN
    assert error.value.location.line == 4


def test_parsing_bounds_nesting_and_source_before_construction(tmp_path: Path) -> None:
    with pytest.raises(ContractError):
        parse_contract(source(tmp_path), limits=ContractLimits(source_bytes=8))
    with pytest.raises(ContractError):
        parse_contract(source(tmp_path, "needs: " + "[" * 1000 + "x" + "]" * 1000))
    with pytest.raises(ContractError):
        parse_contract(
            source(
                tmp_path,
                "produces: out\nverify: {ok: 'true'}\nneeds: ["
                + ", ".join(f"in{i}" for i in range(17))
                + "]",
            )
        )
    with pytest.raises(ContractError):
        parse_contract(
            source(
                tmp_path,
                "produces: out\nverify:\n" + "".join(f"  c{i}: 'true'\n" for i in range(9)),
            )
        )


def test_plan_reads_without_native_probe_install_config_or_writes(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = source(project)
    forbidden = Mock(side_effect=AssertionError("side effect during plan"))
    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr(
        "apm_cli.runtime.copilot_runtime.CopilotRuntime.get_runtime_info", forbidden
    )
    monkeypatch.setattr("apm_cli.config.ensure_config_exists", forbidden)
    monkeypatch.setattr("requests.Session.request", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    before = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    plan = plan_contract(path, project, harness="copilot", model=None)
    after = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    assert before == after
    assert plan.executable_version is None
    assert plan.model is None
    assert plan.manifest_digest == hashlib.sha256((project / "apm.yml").read_bytes()).hexdigest()
    forbidden.assert_not_called()


@pytest.mark.parametrize("name", ["missing.txt", "folder", "alias.txt"])
def test_needs_must_exist_as_regular_non_symlink_files(project: Path, name: str) -> None:
    (project / "folder").mkdir()
    (project / "real.txt").write_text("input", encoding="utf-8")
    (project / "alias.txt").symlink_to(project / "real.txt")
    with pytest.raises(ContractError):
        plan_contract(
            source(project, f"needs: {name}\nproduces: out\nverify: {{ok: 'true'}}"),
            project,
            harness="copilot",
        )


def test_paths_are_project_relative_not_contract_relative(project: Path) -> None:
    (project / "sub").mkdir()
    (project / "notes.txt").write_text("notes", encoding="utf-8")
    path = source(project / "sub", "needs: notes.txt\nproduces: result/out\nverify: {ok: 'true'}")
    plan = plan_contract(path, project, harness="copilot", model="gpt-6-astra")
    assert plan.contract.needs == ("notes.txt",)
    assert plan.project_root == project
    assert plan.model == "gpt-6-astra"


@pytest.mark.parametrize("output", ["notes.txt", "checks/result", "apm.yml", "work.contract.md"])
def test_output_cannot_overlap_selected_inputs(project: Path, output: str) -> None:
    (project / "notes.txt").write_text("notes", encoding="utf-8")
    with pytest.raises(ContractError):
        plan_contract(
            source(project, f"needs: notes.txt\nproduces: {output}\nverify: {{ok: 'true'}}"),
            project,
            harness="copilot",
        )


def test_source_cannot_escape_project(project: Path, tmp_path: Path) -> None:
    other = project / "sub"
    other.mkdir()
    with pytest.raises(ContractError):
        plan_contract(source(project), other, harness="copilot")


@pytest.mark.parametrize("harness", ["codex", "unknown"])
def test_explicit_harness_never_falls_back(project: Path, harness: str) -> None:
    with pytest.raises(ContractError) as error:
        plan_contract(source(project), project, harness=harness)
    assert error.value.code == "unsupported_harness"


def test_missing_executable_is_halted(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apm_cli.runtime.utils.find_runtime_binary", lambda name: None)
    with pytest.raises(ContractError) as error:
        plan_contract(source(project), project, harness="copilot")
    assert error.value.outcome == Outcome.HALTED
    assert error.value.code == "runtime_missing"


@pytest.mark.parametrize("raw", [b"Body only", b"---\nproduces: out", b"---\n{}\n---\n ", b"\xff"])
def test_malformed_or_empty_source_is_halted(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "bad.contract.md"
    path.write_bytes(raw)
    with pytest.raises(ContractError) as error:
        parse_contract(path)
    assert error.value.outcome == Outcome.HALTED
    assert error.value.location.path == path


def test_source_and_output_symlink_are_rejected(project: Path) -> None:
    path = source(project)
    alias = project / "alias.contract.md"
    alias.symlink_to(path)
    with pytest.raises(ContractError):
        plan_contract(alias, project, harness="copilot")
    (project / "out.json").symlink_to(project / "missing")
    with pytest.raises(ContractError):
        plan_contract(path, project, harness="copilot")


def test_source_traversal_is_rejected_even_when_it_lands_inside_root(project: Path) -> None:
    path = source(project)
    (project / "sub").mkdir()
    with pytest.raises(ContractError):
        plan_contract(project / "sub" / ".." / path.name, project, harness="copilot")


def test_windows_execution_refuses_before_native_resolution(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("apm_cli.contracts.frontend.sys.platform", "win32")
    with pytest.raises(ContractError) as error:
        plan_contract(source(project), project, harness="copilot")
    assert error.value.outcome == Outcome.UNPROVEN
    assert error.value.code == "unsupported_platform"
