"""Native requests use supervised, transient merged MCP inventory."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from apm_cli.contracts.models import (
    BaselineSnapshot,
    CheckSpec,
    ContractError,
    ContractLimits,
    ImportedSkill,
    LeafContract,
    LeafPlan,
    Outcome,
    ProcessObservation,
)
from apm_cli.runtime.copilot_runtime import CopilotRuntime
from apm_cli.runtime.factory import RuntimeFactory
from apm_cli.runtime.registry import get_runtime_descriptor

pytestmark = pytest.mark.component


def fake_inventory(
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes = b'{"mcpServers":{}}',
    observation: ProcessObservation | None = None,
) -> Mock:
    def supervise(request, *, on_bytes, limits):
        on_bytes("stderr", b"never-retain-stderr-secret")
        for index in range(0, len(raw), 97):
            on_bytes("stdout", raw[index : index + 97])
        return observation or ProcessObservation(returncode=0)

    mocked = Mock(side_effect=supervise)
    monkeypatch.setattr("apm_cli.contracts.process.supervise_process", mocked)
    return mocked


def inventory(tmp_path: Path, *, timeout_seconds: float = 20) -> tuple[str, ...]:
    return CopilotRuntime.get_contract_mcp_server_names(
        tmp_path / "native",
        tmp_path,
        timeout_seconds=timeout_seconds,
        env={"ORDINARY": "unchanged"},
        limits=ContractLimits(),
    )


@pytest.mark.parametrize("model", [None, "gpt-6-astra"])
def test_native_request_exact_permissions_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str | None
) -> None:
    monkeypatch.setattr(CopilotRuntime, "is_available", staticmethod(lambda: True))
    for key in (
        "COPILOT_ALLOW_ALL",
        "COPILOT_ALLOW_ALL_TOOLS",
        "COPILOT_ALLOW_ALL_PATHS",
        "COPILOT_ALLOW_ALL_URLS",
        "COPILOT_ASSISTED_APPROVAL",
    ):
        monkeypatch.setenv(key, "true")
    monkeypatch.setenv("KEEP_ORDINARY_ENV", "retained")
    supervised = fake_inventory(
        monkeypatch,
        b'{"mcpServers":{"fixture-plugin":{"source":"plugin","env":{"SECRET":"never-retain"}},'
        b'"fixture-user":{"source":"user"}}}',
    )
    clock = Mock(side_effect=[100.0, 102.0])
    monkeypatch.setattr("apm_cli.runtime.copilot_runtime.time.monotonic", clock)
    forbidden = Mock(side_effect=AssertionError("unmanaged native or legacy request"))
    monkeypatch.setattr(CopilotRuntime, "execute_prompt", forbidden)
    monkeypatch.setattr(CopilotRuntime, "get_runtime_info", forbidden)
    monkeypatch.setattr(CopilotRuntime, "get_mcp_config_path", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    contract = LeafContract(
        path=tmp_path / "leaf.contract.md",
        source_digest="raw",
        body="Write the handoff.",
        needs=("notes.md",),
        produces="handoff.json",
        checks=(CheckSpec("handoff", "true"),),
    )
    context = ImportedSkill(
        name="handoff-style",
        source_path=tmp_path / "SKILL.md",
        content="Always include the special marker.",
        source_digest="exact-skill",
        lock_identity="local:../handoff-style",
    )
    plan = LeafPlan(
        contract=contract,
        project_root=tmp_path,
        executable=tmp_path / "resolved-copilot",
        model=model,
        imported_skills=(context,),
    )
    snapshot = BaselineSnapshot(
        root=tmp_path / "baseline",
        producer=tmp_path / "producer",
        files=(),
        digest="base",
        original_head=None,
        synthetic_head="head",
        resources_digest="resources",
    )
    runtime = RuntimeFactory.get_runtime_by_name("copilot", model)
    request = runtime.build_contract_request(plan, snapshot, tmp_path / "run", timeout_seconds=42)
    assert request.cwd == snapshot.producer
    assert request.timeout_seconds == 40
    assert request.argv[:2] == (str(plan.executable), "-p")
    prompt = request.argv[2]
    for selected in (
        contract.body,
        context.content,
        context.source_digest,
        "notes.md",
        "handoff.json",
    ):
        assert selected in prompt
    assert "Use view to read and apply_patch to write." in prompt
    assert (
        "Send brief progress updates in plain ASCII before reading inputs and writing the output."
        in prompt
    )
    tail = request.argv[3:]
    assert tail[:4] == ("--output-format", "json", "--stream", "on")
    assert tail[tail.index("--available-tools") + 1 : tail.index("--available-tools") + 3] == (
        "view",
        "apply_patch",
    )
    assert tail[tail.index("--allow-tool") + 1] == f"write({snapshot.producer / 'handoff.json'})"
    assert tail.count("--allow-tool") == 1
    disabled = [
        tail[index + 1] for index, flag in enumerate(tail) if flag == "--disable-mcp-server"
    ]
    assert disabled == ["fixture-plugin", "fixture-user"]
    assert request.control_observations["disabled_configured_mcp_servers"] == tuple(disabled)
    scope = request.control_observations["startup_scope"]
    assert "User, Workspace, Plugin and Builtin" in scope
    assert "not isolated" in scope
    assert "never-retain" not in str(request.control_observations)
    assert tail.count("--deny-tool") == 2
    assert "shell" in tail and "url" in tail
    for flag in (
        "--no-color",
        "--no-auto-update",
        "--no-remote-export",
        "--no-ask-user",
        "--no-bash-env",
        "--disable-builtin-mcps",
        "--no-custom-instructions",
        "--disallow-temp-dir",
    ):
        assert flag in tail
    assert tail[tail.index("--log-level") + 1] == "none"
    assert not any("allow-all" in item or "add-dir" in item for item in tail)
    assert request.env["KEEP_ORDINARY_ENV"] == "retained"
    assert not any(name.startswith("COPILOT_ALLOW_") for name in request.env)
    assert "COPILOT_ASSISTED_APPROVAL" not in request.env
    if model is None:
        assert "--model" not in request.argv
    else:
        assert request.argv[-2:] == ("--model", model)
    native = supervised.call_args.args[0]
    assert native.argv == (
        str(plan.executable),
        "--no-auto-update",
        "--no-remote-export",
        "--log-level",
        "none",
        "--no-color",
        "--no-bash-env",
        "mcp",
        "list",
        "--json",
    )
    assert native.cwd == snapshot.producer
    assert native.timeout_seconds == 10
    assert native.env == request.env
    assert not native.control_observations
    assert set(supervised.call_args.kwargs) == {"on_bytes", "limits"}
    forbidden.assert_not_called()


def test_runtime_capability_is_owned_by_registry() -> None:
    assert get_runtime_descriptor("copilot").supports_contracts
    for name in ("codex", "llm", "gemini"):
        assert not get_runtime_descriptor(name).supports_contracts


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff",
        b"{malformed-secret-never-display",
        b'{"mcpServers":{"truncated":',
        b'{"mcpServers":{}}\ntrailing',
        b"[]",
        b"{}",
        b'{"servers":{}}',
        b'{"mcpServers":[]}',
        b'{"mcpServers":{},"mcpServers":{"other":{}}}',
        b'{"mcpServers":{"same":{},"same":{}}}',
        b'{"mcpServers":{"--allow-all":{}}}',
        b'{"mcpServers":{"*":{}}}',
        b'{"mcpServers":{"empty":null}}',
        b'{"mcpServers":{"invalid":{"number":NaN}}}',
        b" " * (256 * 1024 + 1),
        b'{"mcpServers":' + b"[" * 1000 + b"0" + b"]" * 1000 + b"}",
        json.dumps({"mcpServers": {f"fixture-{index}": {} for index in range(129)}}).encode(),
    ],
)
def test_unobservable_inventory_refuses_without_retaining_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes, capsys
) -> None:
    fake_inventory(monkeypatch, raw)
    with pytest.raises(ContractError) as error:
        inventory(tmp_path)
    assert error.value.code == "native_mcp_unobservable"
    assert error.value.outcome == Outcome.UNPROVEN
    assert "mcp list --json" in str(error.value)
    assert "malformed-secret" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "observation",
    [
        pytest.param(ProcessObservation(returncode=1), id="unsupported-command"),
        pytest.param(
            ProcessObservation(returncode=None, error="private-spawn-error"),
            id="missing-executable",
        ),
        ProcessObservation(returncode=0, stop_reason="timeout"),
        ProcessObservation(returncode=0, stop_reason="cancelled"),
        ProcessObservation(returncode=0, stop_reason="lingering_children"),
        ProcessObservation(returncode=0, cleanup_confirmed=False),
        ProcessObservation(returncode=0, signals=("SIGTERM",)),
    ],
)
def test_inventory_operational_failure_never_launches_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observation: ProcessObservation
) -> None:
    supervised = fake_inventory(monkeypatch, observation=observation)
    with pytest.raises(ContractError) as error:
        inventory(tmp_path)
    assert error.value.outcome == Outcome.HALTED
    assert error.value.code == "native_mcp_inventory_failed"
    assert "private-spawn-error" not in str(error.value)
    supervised.assert_called_once()


def test_inventory_empty_is_positive_and_timeout_is_clipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervised = fake_inventory(monkeypatch)
    assert inventory(tmp_path, timeout_seconds=2.5) == ()
    assert supervised.call_args.args[0].timeout_seconds == 2.5


def test_inventory_queries_fresh_merged_names_each_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_inventory(monkeypatch, b'{"mcpServers":{"before":{}}}')
    assert inventory(tmp_path) == ("before",)
    fake_inventory(monkeypatch, b'{"mcpServers":{"after":{}}}')
    assert inventory(tmp_path) == ("after",)
