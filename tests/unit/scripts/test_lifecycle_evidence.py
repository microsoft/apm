"""Fail-closed native-provider regressions; no full lifecycle campaign runs here.

Observer test mechanics adapted from be73ed139f3a73c19437a895dfd6d68e41df8bfc.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import check_lifecycle_evidence as gate
from scripts.lifecycle_contracts import (
    CONTRACT_ID,
    LEDGER,
    REQUIRED_WITNESSES,
    EvidenceError,
    candidate_contract,
    command_inventory,
    command_path,
    load_ledger,
    validate_contracts,
    validate_execution,
)
from tests.utils.isolated_apm_environment import DURABLE_ENVIRONMENT_ROOTS, IsolatedApmEnvironment
from tests.utils.lifecycle_evidence import LifecycleEvidencePlugin, _physical_roots, snapshot

pytestmark = pytest.mark.component
pytest_plugins = ["pytester"]


def _ledger() -> dict[str, Any]:
    return load_ledger((gate.ROOT / LEDGER).read_text(encoding="utf-8"))


def _sample() -> tuple[dict[str, Any], dict[str, Any]]:
    witness = {
        "nodeid": "tests/sample.py::test_generated[project]",
        "kind": "generated",
        "dimensions": {"variant": "project"},
        "transitions": [
            {"command": "install", "argv_contains": [], "returncode": 0, "state": state}
            for state in ("changed", "unchanged")
        ],
    }
    evidence = {
        "collected": True,
        "dimensions": {"variant": "project"},
        "phases": {"setup": "passed", "call": "passed", "teardown": "passed"},
        "models": 1,
        "events": [
            {
                "command": "install",
                "args": ["install"],
                "returncode": 0,
                "cwd": "/fixture",
                "roots": {"workspace": "/fixture", "HOME": "/fixture/home"},
                "environment": {"HOME": "/fixture/home"},
                "context": "initial",
                "model": 1,
                "before": before,
                "after": after,
            }
            for before, after in (("a", "b"), ("b", "b"))
        ],
    }
    return witness, evidence


def test_immutable_witnesses_and_current_inventory() -> None:
    """Pin obligations independently of both implementation constants and ledger rows."""
    required = "tests/integration/test_required_lifecycle_state_machine.py::"
    generated = "tests/integration/test_generated_lifecycle_state_machine.py::"
    expected = {
        required + "test_required_global_audit_rule_matrix_for_external_roots[False]",
        required + "test_required_global_audit_rule_matrix_for_external_roots[True]",
        required + "test_required_frozen_semver_transport_refusal_recovers",
        *(
            generated + function + f"[{variant}]"
            for function in (
                "test_generated_lifecycle_mandatory_replay",
                "test_generated_lifecycle_sequences_preserve_reference_model",
            )
            for variant in ("project", "global-canonical", "global-aliased")
        ),
    }
    contract = validate_contracts(_ledger(), command_inventory())[CONTRACT_ID]
    assert set(REQUIRED_WITNESSES) == expected
    assert {w["nodeid"] for w in contract["witnesses"]} == expected
    schema = json.loads((gate.ROOT / "tests/fixtures/lifecycle_completion.schema.json").read_text())
    assert set(schema["properties"]["witnesses"]["items"]["enum"]) == expected
    inventory = command_inventory()
    assert set(contract["commands"]) == set(inventory)
    assert len(inventory) == 86
    assert sum(e["disposition"] == "applicable" for e in contract["commands"].values()) == 22
    assert (
        command_path(["lock", "--global", "export", "--format", "spdx"], inventory) == "lock export"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "delete",
        "duplicate",
        "nodeid",
        "kind",
        "dimension",
        "dimension-type",
        "command",
        "reason",
        "refs",
        "trajectory",
    ],
)
def test_contract_edits_cannot_vacuously_remove_proof(mutation: str) -> None:
    ledger = _ledger()
    contract = ledger["lifecycle_contracts"][0]
    if mutation == "none":
        ledger["lifecycle_contracts"] = []
    elif mutation == "delete":
        contract["witnesses"].pop()
    elif mutation == "duplicate":
        contract["witnesses"][-1] = copy.deepcopy(contract["witnesses"][0])
    elif mutation == "nodeid":
        contract["witnesses"][-1]["nodeid"] = (
            "tests/integration/test_required_lifecycle_state_machine.py::test_unreviewed_semver_witness"
        )
    elif mutation == "kind":
        contract["witnesses"][3]["kind"] = "deterministic"
    elif mutation == "dimension":
        contract["witnesses"][0]["dimensions"] = {}
    elif mutation == "dimension-type":
        contract["witnesses"][-2]["dimensions"] = {"aliased_home": 1}
    elif mutation == "command":
        del contract["commands"]["discover"]
    elif mutation == "reason":
        contract["commands"]["discover"]["reason"] = ""
    elif mutation == "refs":
        contract["commands"]["install"]["generated"] = []
    else:
        contract["witnesses"][0]["transitions"] = []
    message = "Exact immutable witness identities required" if mutation == "nodeid" else None
    with pytest.raises(EvidenceError, match=message):
        validate_contracts(ledger, command_inventory())


@pytest.mark.parametrize("field", ["property_catalog", "bugs", "known_gaps"])
def test_legacy_records_cannot_disappear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    original = json.loads((gate.ROOT / LEDGER).read_text())
    current = copy.deepcopy(original)
    current[field].pop()
    path = tmp_path / LEDGER
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(current))
    monkeypatch.setattr("scripts.lifecycle_contracts.git", lambda *_: json.dumps(original))
    with pytest.raises(EvidenceError, match="legacy"):
        candidate_contract(tmp_path, "a" * 40, command_inventory())


@pytest.mark.parametrize(
    "mutation",
    [
        "collection",
        "skip",
        "xfail",
        "setup",
        "call",
        "teardown",
        "dimension",
        "generation",
        "missing-transition",
        "status",
        "command",
        "args",
        "workspace",
        "state",
        "state-missing",
        "outside-model",
        "roots",
        "continuity",
        "model-splice",
        "environment",
        "context",
    ],
)
def test_execution_obligations_fail_closed(mutation: str) -> None:
    witness, evidence = _sample()
    validate_execution(witness, evidence)
    event = evidence["events"][-1]
    if mutation == "collection":
        evidence["collected"] = False
    elif mutation in {"skip", "xfail"}:
        evidence["phases"]["call"] = mutation
    elif mutation in {"setup", "call", "teardown"}:
        evidence["phases"][mutation] = "failed"
    elif mutation == "dimension":
        evidence["dimensions"] = {}
    elif mutation == "generation":
        evidence["models"] = 0
    elif mutation == "missing-transition":
        evidence["events"].pop()
    elif mutation == "status":
        event["returncode"] = 1
    elif mutation == "command":
        event["command"] = "update"
    elif mutation == "args":
        witness["transitions"][-1]["argv_contains"] = ["--global"]
    elif mutation == "workspace":
        event["cwd"] = "/elsewhere"
    elif mutation == "state":
        event["after"] = "c"
    elif mutation == "state-missing":
        del event["before"]
    elif mutation == "outside-model":
        event["model"] = None
    elif mutation == "roots":
        event["roots"] = {}
    elif mutation == "continuity":
        event.update(before="c", after="c")
    elif mutation == "model-splice":
        evidence["models"] = 2
        event["model"] = 2
    elif mutation == "environment":
        event["environment"]["HOME"] = "/different/home"
    else:
        event["context"] = "other"
    with pytest.raises(EvidenceError):
        validate_execution(witness, evidence)


def test_preparation_and_reviewed_context_preserve_connected_domain() -> None:
    witness, evidence = _sample()
    for event in evidence["events"]:
        event["roots"]["APM_HOME"] = "/fixture/home/.apm"
        event["environment"]["APM_HOME"] = "/fixture/home/.apm"
    event = evidence["events"][-1]
    event.update(cwd="/fixture/home/.apm", context="APM_HOME", before="prepared", after="prepared")
    transition = witness["transitions"][-1]
    transition["context"] = "APM_HOME"
    with pytest.raises(EvidenceError, match="trajectory"):
        validate_execution(witness, evidence)
    transition["preparation"] = "A reviewed fixture mutation changes the declaration."
    validate_execution(witness, evidence)


def test_snapshot_and_isolation_share_the_existing_durable_root_owner(tmp_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "domain", base_env={})
    env = isolated.subprocess_env()
    assert all(Path(env[key]).is_dir() for key in DURABLE_ENVIRONMENT_ROOTS)
    domain = {key: env[key] for key in DURABLE_ENVIRONMENT_ROOTS}
    before = snapshot(domain)
    user = isolated.home / "unowned"
    user.write_text("user bytes")
    assert snapshot(domain) != before
    assert "APM_TEMP_DIR" not in DURABLE_ENVIRONMENT_ROOTS


def test_domains_do_not_splice_roots_models_or_lexical_aliases(tmp_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "domain", base_env={})
    env = isolated.subprocess_env()
    plugin = LifecycleEvidencePlugin(["node"], None, {"node": {"APM_HOME"}})
    plugin.active = "node"
    roots, lexical, context = plugin._domain(isolated.work_root, env)
    assert context == "initial"
    assert lexical["HOME"] == str(isolated.home)
    assert plugin._domain(isolated.config_root, env) == (roots, lexical, "APM_HOME")
    changed = {**env, "HOME": str(tmp_path / "other-home")}
    with pytest.raises(EvidenceError, match="durable roots changed"):
        plugin._domain(isolated.work_root, changed)
    with pytest.raises(EvidenceError, match="Unreviewed"):
        plugin._domain(isolated.repository_root, env)
    plugin.model_run = 2
    other, _, _ = plugin._domain(isolated.work_root, changed)
    assert other != roots


def test_child_root_and_source_probes_use_actual_child_environment(tmp_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "domain", base_env=os.environ)
    env = isolated.subprocess_env()
    env["CLAUDE_CONFIG_DIR"] = "~/custom"
    roots = _physical_roots(isolated.work_root, env)
    assert roots["CLAUDE_CONFIG_DIR"] == str(isolated.home / "custom")
    plugin = LifecycleEvidencePlugin([], None)
    assert plugin._source_identity(isolated.work_root, env, 30)["package"] == str(
        gate.ROOT / "src/apm_cli"
    )
    foreign = tmp_path / "foreign/apm_cli"
    foreign.mkdir(parents=True)
    (foreign / "__init__.py").write_text("")
    (foreign / "cli.py").write_text("print('success is not source identity')")
    env["PYTHONPATH"] = str(foreign.parent)
    with pytest.raises(EvidenceError, match="candidate checkout"):
        plugin._source_identity(isolated.work_root, env, 30)


@pytest.mark.parametrize("mutation", ["command", "interpreter", "launcher"])
def test_runner_observer_refuses_unverified_execution(tmp_path: Path, mutation: str) -> None:
    from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner

    launcher = tmp_path / "apm"
    launcher.write_text("fixture launcher")
    plugin = LifecycleEvidencePlugin(["node"], launcher)
    plugin.active, plugin.phase = "node", "call"
    plugin.pytest_sessionstart(SimpleNamespace())
    try:
        command = (sys.executable, "-m", "apm_cli.cli")
        if mutation == "command":
            command = (sys.executable, "-c", "print('not APM')")
        elif mutation == "interpreter":
            plugin.python_hash = "changed"
        else:
            command = (str(launcher),)
            launcher.write_text("changed launcher")
        with pytest.raises(EvidenceError, match=r"Unverified|Executable changed"):
            ApmLifecycleRunner(command).run(["--version"], cwd=tmp_path, env={})
    finally:
        plugin.patch.undo()


@pytest.mark.parametrize("response", ["[]", '{"CLAUDE_CONFIG_DIR":0}', '{"other":"/path"}'])
def test_root_expansion_rejects_malformed_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=response)
    )
    with pytest.raises(EvidenceError, match="invalid paths"):
        _physical_roots(tmp_path, {"CLAUDE_CONFIG_DIR": "~/custom"})


@pytest.mark.parametrize("mutation", ["head", "tree", "dirty", "untracked", "replace", "abbrev"])
def test_candidate_identity_refuses_stale_or_dirty_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    base, head = "a" * 40, "b" * 40
    answers = {
        ("for-each-ref", "--format=%(refname)", "refs/replace"): "",
        ("rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"): base,
        ("rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}"): head,
        ("rev-parse", "HEAD"): head,
        ("merge-base", "--is-ancestor", base, head): "",
        ("status", "--porcelain", "--untracked-files=all"): "",
        ("rev-parse", "HEAD^{tree}"): "c" * 40,
    }
    monkeypatch.setattr(gate, "git", lambda root, *args: answers[args])
    initial = gate.candidate(tmp_path, base, head)
    if mutation == "head":
        answers[("rev-parse", "HEAD")] = "d" * 40
    elif mutation == "tree":
        answers[("rev-parse", "HEAD^{tree}")] = "d" * 40
        assert gate.candidate(tmp_path, base, head) != initial
        return
    elif mutation in {"dirty", "untracked"}:
        answers[("status", "--porcelain", "--untracked-files=all")] = (
            " M tracked.py" if mutation == "dirty" else "?? new.py"
        )
    elif mutation == "replace":
        answers[("for-each-ref", "--format=%(refname)", "refs/replace")] = "refs/replace/hash"
    else:
        answers[("rev-parse", "--verify", "--end-of-options", "main^{commit}")] = base
        base = "main"
    with pytest.raises(EvidenceError):
        gate.candidate(tmp_path, base, head)


def test_source_profile_pins_checkout_launcher_and_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apm_cli

    source = tmp_path / "src/apm_cli"
    source.mkdir(parents=True)
    (source / "cli.py").write_text("def main():\n    return 0\n")
    monkeypatch.setattr(apm_cli, "__file__", str(source / "__init__.py"))
    python = tmp_path / "python"
    python.write_text("fixture interpreter")
    monkeypatch.setattr(sys, "executable", str(python))
    assert gate.source_profile(tmp_path)[0] is None
    foreign = tmp_path / "foreign/src/apm_cli"
    foreign.mkdir(parents=True)
    (foreign / "__init__.py").write_text("")
    (foreign / "cli.py").write_bytes((source / "cli.py").read_bytes())
    if os.name != "nt":
        (tmp_path / "apm").write_text(f"#!{python}\nfrom apm_cli.cli import main\nmain()\n")
    with pytest.raises(EvidenceError, match="candidate checkout"):
        gate.source_profile(tmp_path / "foreign")
    if os.name != "nt":
        launcher = tmp_path / "apm"
        launcher.write_text("#!/foreign/python\nfrom apm_cli.cli import cli\n")
        with pytest.raises(EvidenceError, match="this Python environment"):
            gate.source_profile(tmp_path)
        launcher.write_text(f"#!{python}\nfrom apm_cli.cli import cli\n")
        initial = gate.source_profile(tmp_path)[1]
        launcher.write_text(f"#!{python}\nfrom apm_cli.cli import cli\n# changed\n")
        assert gate.source_profile(tmp_path)[1] != initial


def test_launcher_cannot_borrow_another_environment_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        assert gate.source_profile(gate.ROOT)[0] is None
        return
    current, foreign = tmp_path / "current", tmp_path / "foreign"
    current.mkdir()
    foreign.mkdir()
    original_python = Path(sys.executable).resolve()
    (current / "python").symlink_to(original_python)
    (foreign / "python").symlink_to(original_python)
    launcher = current / "apm"
    launcher.write_text(f"#!{foreign / 'python'}\nfrom apm_cli.cli import cli\n")
    monkeypatch.setattr(sys, "executable", str(current / "python"))
    assert (current / "python").samefile(foreign / "python")
    with pytest.raises(EvidenceError, match="this Python environment"):
        gate.source_profile(gate.ROOT)


@pytest.mark.parametrize(
    "variant",
    [
        "single-quoted",
        "double-quoted",
        "unquoted",
        "foreign-environment",
        "different-interpreter",
        "relative-interpreter",
        "empty-interpreter",
        "unterminated-path",
        "missing-terminator",
        "extra-shell-line",
        "extra-shell-command",
        "extra-python-argument",
        "command-substitution",
        "variable-expansion",
        "wrong-forwarding",
        "missing-cli",
    ],
)
def test_source_profile_accepts_only_canonical_long_path_launchers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    from pip._vendor.distlib.scripts import ScriptMaker

    if os.name == "nt":
        assert gate.source_profile(gate.ROOT)[0] is None
        return
    directory = tmp_path / ("x" * 170) / ("y" * 170) / ("z" * 170)
    directory.mkdir(parents=True)
    python = directory / "python"
    python.symlink_to(Path(sys.executable).resolve())
    assert len(str(python)) > 512
    quoted = {
        "double-quoted": f'"{python}"',
        "unquoted": str(python),
    }.get(variant, f"'{python}'")
    maker = ScriptMaker(None, str(directory))
    maker.executable = quoted
    maker.variants = {""}
    launcher = directory / "apm"
    assert maker.make("apm = apm_cli.cli:main") == [str(launcher)]
    script = launcher.read_text(encoding="utf-8")
    assert script.startswith("#!/bin/sh\n'''exec' ")
    monkeypatch.setattr(sys, "executable", str(python))
    executable, profile = gate.source_profile(gate.ROOT)
    assert executable == launcher
    assert profile["python_environment"] == str(directory.resolve())
    assert profile["executable_sha256"] == hashlib.sha256(launcher.read_bytes()).hexdigest()
    if variant in {"single-quoted", "double-quoted", "unquoted"}:
        return

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    foreign_python = foreign / "python"
    foreign_python.symlink_to(python.resolve())
    different_python = directory / "python-untrusted"
    different_python.write_text("not this interpreter")
    assert different_python.parent == python.parent
    assert str(different_python).startswith(str(python))
    assert not different_python.samefile(python)
    substitutions = {
        "foreign-environment": (quoted, f"'{foreign_python}'"),
        "different-interpreter": (quoted, f"'{different_python}'"),
        "relative-interpreter": (quoted, "'python'"),
        "empty-interpreter": (quoted, "''"),
        "unterminated-path": (quoted, f"'{python}"),
        "missing-terminator": ("\n' '''\n", "\n"),
        "extra-shell-line": ("#!/bin/sh\n", "#!/bin/sh\ntrue\n"),
        "extra-shell-command": (' "$0" "$@"\n', ' "$0" "$@"; true\n'),
        "extra-python-argument": (' "$0" "$@"\n', ' -I "$0" "$@"\n'),
        "command-substitution": (quoted, f'"{python}$(true)"'),
        "variable-expansion": (quoted, f'"{python}$UNSET"'),
        "wrong-forwarding": (' "$0" "$@"\n', ' "$@" "$0"\n'),
        "missing-cli": ("apm_cli.cli", "foreign.cli"),
    }
    old, new = substitutions[variant]
    changed = script.replace(old, new, 1)
    assert changed != script
    launcher.write_text(changed, encoding="utf-8")
    with pytest.raises(EvidenceError, match="this Python environment"):
        gate.source_profile(gate.ROOT)


def _records(contract: dict[str, Any]) -> dict[str, Any]:
    """Fabricate connected observations for orchestration tests, not native proof."""
    records = {}
    for witness in contract["witnesses"]:
        events, state = [], 0
        for transition in witness["transitions"]:
            before = str(state)
            if transition["state"] != "unchanged":
                state += 1
            context = transition.get("context", "initial")
            events.append(
                {
                    **transition,
                    "args": transition["argv_contains"],
                    "before": before,
                    "after": str(state),
                    "context": context,
                    "cwd": "/fixture" if context == "initial" else "/home/.apm",
                    "roots": {"workspace": "/fixture", "HOME": "/home", "APM_HOME": "/home/.apm"},
                    "environment": {"HOME": "/home", "APM_HOME": "/home/.apm"},
                    "model": 1,
                }
            )
        records[witness["nodeid"]] = {
            "collected": True,
            "dimensions": witness["dimensions"],
            "models": 1,
            "events": events,
            "phases": {"setup": "passed", "call": "passed", "teardown": "passed"},
        }
    return records


def _completion(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    native = {
        "version": 1,
        "contract_id": CONTRACT_ID,
        "base": "a" * 40,
        "head": "b" * 40,
        "tested_tree": "c" * 40,
        "lane": "full",
        "status": "passed",
        "witnesses": _records(_ledger()["lifecycle_contracts"][0]),
        "profile": {
            "kind": "source-python",
            "cli_sha256": "f" * 64,
            "source_root": "/driver/src/apm_cli",
            "python": "/driver/python",
            "python_environment": "/driver/venv/bin",
            "python_sha256": "e" * 64,
            "executable": "/driver/venv/bin/apm",
            "executable_sha256": "d" * 64,
        },
    }
    driver = tmp_path / "driver.json"
    driver.write_text(json.dumps(native))
    summary = {
        "version": 1,
        "contract_id": CONTRACT_ID,
        "base_sha": native["base"],
        "head_sha": native["head"],
        "tested_tree": native["tested_tree"],
        "lane": "full",
        "status": "passed",
        "report_path": str(driver),
        "report_sha256": hashlib.sha256(driver.read_bytes()).hexdigest(),
        "witnesses": list(REQUIRED_WITNESSES),
    }
    path = tmp_path / "completion.json"
    path.write_text(json.dumps(summary))
    native["profile"]["source_root"] = "/independent-checkout"
    native["profile"].update(
        python="/independent/python",
        python_environment="/independent/venv/bin",
        python_sha256="c" * 64,
        executable=None,
        executable_sha256=None,
    )
    return path, summary, native


@pytest.fixture
def external_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep component artifacts in the project while modelling an external checkout."""
    checkout = tmp_path / "checkout"
    schema = checkout / "tests/fixtures/lifecycle_completion.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_bytes((gate.ROOT / "tests/fixtures/lifecycle_completion.schema.json").read_bytes())
    ledger = (gate.ROOT / LEDGER).read_text(encoding="utf-8")
    (checkout / LEDGER).write_text(ledger, encoding="utf-8")
    monkeypatch.setattr("scripts.lifecycle_contracts.git", lambda *_: ledger)
    monkeypatch.setattr(gate, "ROOT", checkout)
    return checkout


@pytest.mark.parametrize(
    "mutation",
    [
        "base_sha",
        "head_sha",
        "tested_tree",
        "lane",
        "status",
        "version",
        "contract_id",
        "report_sha256",
        "report_path",
        "witnesses",
        "extra-field",
        "driver-head",
        "driver-witness",
        "source",
        "profile",
        "schema",
    ],
)
def test_completion_rejects_stale_and_malformed_claims(
    tmp_path: Path, external_checkout: Path, mutation: str
) -> None:
    path, summary, native = _completion(tmp_path)
    output = tmp_path / "independent.json"
    gate.validate_completion(path, native, output)
    if mutation in {"base_sha", "head_sha", "tested_tree", "report_sha256"}:
        summary[mutation] = "d" * len(summary[mutation])
    elif mutation == "version":
        summary["version"] = True
    elif mutation == "witnesses":
        summary["witnesses"].pop()
    elif mutation == "extra-field":
        summary["lifecycle_evidence"] = {}
    elif mutation.startswith("driver") or mutation in {"source", "profile"}:
        driver = Path(summary["report_path"])
        data = json.loads(driver.read_text())
        if mutation == "driver-head":
            data["head"] = "d" * 40
        elif mutation == "driver-witness":
            data["witnesses"].pop(REQUIRED_WITNESSES[0])
        elif mutation == "source":
            data["profile"]["cli_sha256"] = "e" * 64
        else:
            data["profile"]["kind"] = "packaged-binary"
        driver.write_text(json.dumps(data))
        summary["report_sha256"] = hashlib.sha256(driver.read_bytes()).hexdigest()
    elif mutation == "schema":
        summary = []
    else:
        summary[mutation] = ""
    path.write_text(json.dumps(summary))
    with pytest.raises(EvidenceError):
        gate.validate_completion(path, native, output)


@pytest.mark.parametrize("mode", ["verify", "emit"])
@pytest.mark.parametrize(
    "mutation",
    ["uncollected", "skipped", "empty-record", "invalid-record", "empty-events", "no-models"],
)
def test_completion_rejects_invalid_execution_even_with_recomputed_digest(
    tmp_path: Path, external_checkout: Path, mode: str, mutation: str
) -> None:
    path, summary, native = _completion(tmp_path)
    driver = Path(summary["report_path"])
    report = json.loads(driver.read_bytes())
    nodeid = next(node for node in REQUIRED_WITNESSES if "sequences_preserve" in node)
    record = report["witnesses"][nodeid]
    if mutation == "uncollected":
        record["collected"] = False
    elif mutation == "skipped":
        record["phases"]["setup"] = "skipped"
    elif mutation in {"empty-record", "invalid-record"}:
        report["witnesses"][nodeid] = {} if mutation == "empty-record" else None
    elif mutation == "empty-events":
        record["events"] = []
    else:
        record["models"] = 0
    raw = gate._json_bytes(report)
    driver.write_bytes(raw)
    summary["report_sha256"] = hashlib.sha256(raw).hexdigest()
    path.write_bytes(gate._json_bytes(summary))
    with pytest.raises(
        EvidenceError, match=r"not collected|must pass|execution record|trajectory|model"
    ):
        if mode == "verify":
            gate.validate_completion(path, native, tmp_path / "independent.json")
        else:
            gate.completion_summary(report, driver, raw)


@pytest.mark.parametrize("mode", ["verify", "emit"])
@pytest.mark.parametrize(
    "mutation",
    [
        "missing-profile",
        "null-profile",
        "list-profile",
        "missing-source_root",
        "missing-python",
        "missing-python_environment",
        "missing-python_sha256",
        "missing-cli_sha256",
        "missing-executable",
        "missing-executable_sha256",
        "relative-source_root",
        "relative-python",
        "relative-python_environment",
        "relative-executable",
        "invalid-python_sha256",
        "invalid-cli_sha256",
        "invalid-executable_sha256",
        "missing-launcher-digest",
        "orphan-launcher-digest",
    ],
)
def test_completion_rejects_malformed_identity_even_with_recomputed_digest(
    tmp_path: Path, external_checkout: Path, mode: str, mutation: str
) -> None:
    path, summary, native = _completion(tmp_path)
    driver = Path(summary["report_path"])
    report = json.loads(driver.read_bytes())
    profile = report["profile"]
    if mutation == "missing-profile":
        del report["profile"]
    elif mutation in {"null-profile", "list-profile"}:
        report["profile"] = None if mutation == "null-profile" else []
    elif mutation == "missing-launcher-digest":
        profile["executable_sha256"] = None
    elif mutation == "orphan-launcher-digest":
        profile["executable"] = None
    else:
        operation, field = mutation.split("-", 1)
        if operation == "missing":
            del profile[field]
        else:
            profile[field] = "relative/path" if operation == "relative" else 64
    raw = gate._json_bytes(report)
    driver.write_bytes(raw)
    summary["report_sha256"] = hashlib.sha256(raw).hexdigest()
    path.write_bytes(gate._json_bytes(summary))
    with pytest.raises(EvidenceError, match=r"profile"):
        if mode == "verify":
            gate.validate_completion(path, native, tmp_path / "independent.json")
        else:
            gate.completion_summary(report, driver, raw)


def test_completion_protects_aliases_and_accepts_independent_source_path(
    tmp_path: Path, external_checkout: Path
) -> None:
    path, summary, native = _completion(tmp_path)
    summary["report_path"] = "driver.json"
    path.write_text(json.dumps(summary))
    gate.validate_completion(path, native, tmp_path / "independent.json")
    driver = tmp_path / "driver.json"
    for kind in ("hardlink", "symlink"):
        alias = tmp_path / kind
        if kind == "hardlink":
            alias.hardlink_to(driver)
        else:
            alias.symlink_to(driver)
        with pytest.raises(EvidenceError, match="overwrite"):
            gate.validate_completion(path, native, alias)
    with pytest.raises(EvidenceError, match="overwrite"):
        gate.validate_completion(path, native, path)


def test_completion_runs_fresh_and_never_overwrites_previous_reports(
    tmp_path: Path, external_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _, native = _completion(tmp_path)
    output = tmp_path / "independent.json"
    calls = []

    def fresh(args: argparse.Namespace) -> dict[str, Any]:
        calls.append((args.base, args.head, args.lane))
        return native

    monkeypatch.setattr(gate, "execute", fresh)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gate",
            "--base",
            native["base"],
            "--head",
            native["head"],
            "--lane",
            "full",
            "--report",
            str(output),
            "--completion",
            str(path),
        ],
    )
    assert gate.main() == 0
    assert calls == [(native["base"], native["head"], "full")]
    before = output.read_bytes()
    assert gate.main() == 1
    assert output.read_bytes() == before
    assert len(calls) == 1


def test_failed_independent_execution_persists_original_failure_before_completion_comparison(
    tmp_path: Path,
    external_checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, summary, native = _completion(tmp_path)
    driver = Path(summary["report_path"])
    preserved = (driver.read_bytes(), path.read_bytes())
    native.update(status="blocked", error="Fresh witness setup failed")
    output = tmp_path / "failed-independent.json"
    monkeypatch.setattr(gate, "execute", lambda args: native)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gate",
            "--base",
            native["base"],
            "--head",
            native["head"],
            "--lane",
            "full",
            "--report",
            str(output),
            "--completion",
            str(path),
        ],
    )
    assert gate.main() == 1
    captured = capsys.readouterr()
    assert json.loads(output.read_bytes()) == native
    assert str(output) in captured.out
    assert "Fresh witness setup failed" in captured.err
    assert "Completion disagrees" not in captured.err
    assert "Lifecycle completion:" not in captured.out
    assert (driver.read_bytes(), path.read_bytes()) == preserved


def test_emitted_sidecar_round_trips_through_fresh_independent_verification(
    tmp_path: Path, external_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, native = _completion(tmp_path)
    report = tmp_path / "fresh-driver.json"
    sidecar = tmp_path / "fresh-completion.json"
    independent = tmp_path / "independent.json"
    calls = []

    def fresh(args: argparse.Namespace) -> dict[str, Any]:
        calls.append(args.report)
        result = copy.deepcopy(native)
        result["profile"]["source_root"] = f"/checkout-{len(calls)}"
        return result

    monkeypatch.setattr(gate, "execute", fresh)
    arguments = ["gate", "--base", native["base"], "--head", native["head"], "--lane", "full"]
    monkeypatch.setattr(
        sys,
        "argv",
        [*arguments, "--report", str(report), "--completion-output", str(sidecar)],
    )
    assert gate.main() == 0
    summary = json.loads(sidecar.read_bytes())
    assert summary["report_path"] == str(report.resolve())
    assert summary["report_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    assert summary["witnesses"] == sorted(REQUIRED_WITNESSES)
    assert sidecar.read_bytes() == gate._json_bytes(summary)
    assert b"\r" not in sidecar.read_bytes()
    assert summary == gate.completion_summary(
        json.loads(report.read_bytes()), report, report.read_bytes()
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [*arguments, "--report", str(independent), "--completion", str(sidecar)],
    )
    assert gate.main() == 0
    assert calls == [report, independent]
    assert json.loads(independent.read_bytes())["profile"]["source_root"] == "/checkout-2"
    assert summary["report_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()


@pytest.mark.parametrize("status", ["blocked", "pending", "not_applicable"])
def test_unsuccessful_execution_never_emits_a_completion(
    tmp_path: Path, external_checkout: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    _, _, native = _completion(tmp_path)
    native.update(status=status, error="negative control")
    report, sidecar = tmp_path / "failed-report.json", tmp_path / "must-not-exist.json"
    monkeypatch.setattr(gate, "execute", lambda args: native)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gate",
            "--base",
            native["base"],
            "--head",
            native["head"],
            "--lane",
            "full",
            "--report",
            str(report),
            "--completion-output",
            str(sidecar),
        ],
    )
    assert gate.main() == 1
    assert json.loads(report.read_bytes())["status"] == status
    assert not sidecar.exists()


@pytest.mark.parametrize(
    "mutation", ["existing", "same-report", "symlink", "hardlink", "in-checkout", "both-modes"]
)
def test_completion_output_refuses_unsafe_paths_before_execution(
    tmp_path: Path, external_checkout: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    report, sidecar = tmp_path / "new-report.json", tmp_path / "new-completion.json"
    sentinel = tmp_path / "preserved.json"
    sentinel.write_bytes(b"preserve these bytes")
    if mutation == "existing":
        sidecar = sentinel
    elif mutation == "same-report":
        sidecar = report
    elif mutation == "symlink":
        sidecar.symlink_to(sentinel)
    elif mutation == "hardlink":
        sidecar.hardlink_to(sentinel)
    elif mutation == "in-checkout":
        sidecar = external_checkout / "forbidden.json"
    arguments = [
        "gate",
        "--base",
        "a" * 40,
        "--head",
        "b" * 40,
        "--lane",
        "full",
        "--report",
        str(report),
        "--completion-output",
        str(sidecar),
    ]
    if mutation == "both-modes":
        arguments.extend(["--completion", str(sentinel)])

    def unexpected(args: argparse.Namespace) -> dict[str, Any]:
        raise AssertionError("Unsafe output must be rejected before native execution")

    monkeypatch.setattr(gate, "execute", unexpected)
    monkeypatch.setattr(sys, "argv", arguments)
    if mutation in {"in-checkout", "both-modes"}:
        with pytest.raises(SystemExit) as error:
            gate.main()
        assert error.value.code == 2
    else:
        assert gate.main() == 1
    assert sentinel.read_bytes() == b"preserve these bytes"
    assert not report.exists()


@pytest.mark.parametrize("mutation", ["witnesses", "profile", "bytes", "schema"])
def test_completion_emission_refuses_inconsistent_success_report(
    tmp_path: Path, external_checkout: Path, mutation: str
) -> None:
    _, _, native = _completion(tmp_path)
    if mutation == "witnesses":
        native["witnesses"].pop(REQUIRED_WITNESSES[0])
    elif mutation == "profile":
        native["profile"]["kind"] = "packaged-binary"
    elif mutation == "schema":
        native["head"] = "not-a-sha"
    raw = gate._json_bytes(native)
    if mutation == "bytes":
        raw = b"{}"
    with pytest.raises(EvidenceError):
        gate.completion_summary(native, tmp_path / "report.json", raw)


@pytest.mark.parametrize(
    "mutation",
    [
        "pass",
        "pytest-failed",
        "missing",
        "skipped-call",
        "dirty-after",
        "head-after",
        "tree-after",
        "source-after",
        "pytest-failed-dirty-after",
        "pytest-failed-source-after",
        "pytest-failed-both-after",
    ],
)
def test_provider_execution_checks_identity_after_its_own_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Unit-only fabricated events exercise orchestration, never native acceptance."""
    contract = _ledger()["lifecycle_contracts"][0]
    records = _records(contract)
    identity = {"base": "a" * 40, "head": "b" * 40, "tested_tree": "c" * 40}
    profile = {"kind": "source-python", "source_root": str(gate.ROOT / "src/apm_cli")}
    calls: list[str] = []

    def candidate(*args: Any) -> dict[str, str]:
        if calls:
            if mutation in {"dirty-after", "pytest-failed-dirty-after", "pytest-failed-both-after"}:
                raise EvidenceError("Candidate dirty after execution")
            if mutation in {"head-after", "tree-after"}:
                field = "head" if mutation == "head-after" else "tested_tree"
                return {**identity, field: "d" * 40}
        return identity

    def source(*args: Any) -> tuple[None, dict[str, str]]:
        if calls and mutation in {
            "source-after",
            "pytest-failed-source-after",
            "pytest-failed-both-after",
        }:
            return None, {**profile, "python": "changed"}
        return None, profile

    def fresh_pytest(args: list[str], plugins: list[Any]) -> int:
        assert args[-9:] == sorted(REQUIRED_WITNESSES)
        assert plugins[0].records is records
        calls.append("fresh pytest")
        if mutation == "missing":
            records.pop(REQUIRED_WITNESSES[0])
        elif mutation == "skipped-call":
            records[REQUIRED_WITNESSES[0]]["phases"]["call"] = "skipped"
        return 1 if mutation.startswith("pytest-failed") else 0

    plugin = SimpleNamespace(records=records, patch=SimpleNamespace(undo=lambda: None))
    monkeypatch.setattr(gate, "candidate", candidate)
    monkeypatch.setattr(gate, "source_profile", source)
    monkeypatch.setattr(gate, "candidate_contract", lambda *args: contract)
    monkeypatch.setattr("tests.utils.lifecycle_evidence.LifecycleEvidencePlugin", lambda *a: plugin)
    monkeypatch.setattr(pytest, "main", fresh_pytest)
    result = gate.execute(
        argparse.Namespace(
            base=identity["base"],
            head=identity["head"],
            lane="full",
            report=tmp_path / "report.json",
        )
    )
    assert calls == ["fresh pytest"]
    assert result["status"] == ("passed" if mutation == "pass" else "blocked")
    if mutation != "pass":
        assert result["error"]
    if mutation == "skipped-call":
        assert set(result["witnesses"]) == set(REQUIRED_WITNESSES)
        assert "setup/call/teardown must pass" in result["error"]
    if mutation.startswith("pytest-failed"):
        assert result["error"] == "Lifecycle pytest execution failed with exit code 1"
    if mutation.startswith("pytest-failed-"):
        expected = []
        if mutation in {"pytest-failed-dirty-after", "pytest-failed-both-after"}:
            expected.append("Candidate dirty after execution")
        if mutation in {"pytest-failed-source-after", "pytest-failed-both-after"}:
            expected.append("Source executable identity changed during execution")
        assert result["postflight_errors"] == expected


@pytest.mark.parametrize("postflight_failure", [False, True])
def test_early_contract_error_preserves_first_cause_without_a_profile_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postflight_failure: bool
) -> None:
    identity = {"base": "a" * 40, "head": "b" * 40, "tested_tree": "c" * 40}
    calls = []

    def candidate(*args: Any) -> dict[str, str]:
        calls.append("candidate")
        if len(calls) > 1 and postflight_failure:
            raise EvidenceError("Candidate genuinely changed after contract failure")
        return identity

    def contract(*args: Any) -> dict[str, Any]:
        raise EvidenceError("Missing reviewed assessment")

    def unexpected_profile(*args: Any) -> Any:
        raise AssertionError("No source baseline was captured")

    monkeypatch.setattr(gate, "candidate", candidate)
    monkeypatch.setattr(gate, "candidate_contract", contract)
    monkeypatch.setattr(gate, "source_profile", unexpected_profile)
    result = gate.execute(
        argparse.Namespace(
            base=identity["base"],
            head=identity["head"],
            lane="full",
            report=tmp_path / "report.json",
        )
    )
    assert result["status"] == "blocked"
    assert result["error"] == "Missing reviewed assessment"
    assert result["witnesses"] == {}
    assert calls == ["candidate", "candidate"]
    assert result.get("postflight_errors", []) == (
        ["Candidate genuinely changed after contract failure"] if postflight_failure else []
    )
    assert not (tmp_path / ".report.json.pytest").exists()


@pytest.mark.parametrize("mode", ["skip", "xfail", "setup", "teardown", "call", "pass"])
def test_observer_records_real_pytest_phases(pytester: pytest.Pytester, mode: str) -> None:
    pytester.makeini("[pytest]")
    module = pytester.makepyfile(f"""
        import pytest
        @pytest.fixture
        def fixture():
            if {mode!r} == "setup":
                raise RuntimeError("setup failed")
            yield
            if {mode!r} == "teardown":
                raise RuntimeError("teardown failed")
        def test_observed(fixture):
            if {mode!r} == "skip":
                pytest.skip("negative control")
            if {mode!r} == "xfail":
                pytest.xfail("negative control")
            if {mode!r} == "call":
                raise RuntimeError("call failed")
            assert 1 == 1
    """)
    nodeid = f"{module.name}::test_observed"
    plugin = LifecycleEvidencePlugin([nodeid], None)
    result = pytester.runpytest_inprocess("-q", "-o", "addopts=", nodeid, plugins=[plugin])
    record = plugin.records[nodeid]
    phase = mode if mode in {"setup", "teardown"} else "call"
    expected = {"pass": "passed", "skip": "skipped", "xfail": "xfail"}.get(mode, "failed")
    assert record["phases"][phase] == expected
    if mode in {"setup", "teardown", "call"}:
        assert result.ret != 0


def test_repeated_phase_cannot_erase_an_earlier_failure() -> None:
    plugin = LifecycleEvidencePlugin(["node"], None)
    plugin.records["node"] = {"phases": {}}
    for outcome in ("failed", "passed", "passed"):
        plugin.pytest_runtest_logreport(
            SimpleNamespace(nodeid="node", when="call", outcome=outcome)
        )
    assert plugin.records["node"]["phases"] == {"call": "repeated"}


@pytest.mark.parametrize("selection", ["missing", "extra", "deselected", "wrong-param"])
def test_observer_requires_exact_collection(pytester: pytest.Pytester, selection: str) -> None:
    pytester.makeini("[pytest]")
    module = pytester.makepyfile("""
        import pytest
        @pytest.mark.parametrize("variant", ["actual"])
        def test_observed(variant):
            assert variant == "actual"
        def test_extra():
            assert 1 == 1
    """)
    nodeid = f"{module.name}::test_observed[actual]"
    desired = nodeid if selection != "wrong-param" else nodeid.replace("[actual]", "[missing]")
    plugin = LifecycleEvidencePlugin([desired], None)
    args = [nodeid]
    if selection == "missing":
        args = [f"{module.name}::missing"]
    elif selection == "extra":
        args = [module.name]
    elif selection == "deselected":
        args += ["-k", "not observed"]
    result = pytester.runpytest_inprocess("-q", "-o", "addopts=", *args, plugins=[plugin])
    assert result.ret != 0


def test_observer_requires_real_runner_and_hypothesis_execution(pytester: pytest.Pytester) -> None:
    """A tiny genuine generated run exercises instrumentation without native scenarios."""
    pytester.makeini("[pytest]")
    module = pytester.makepyfile("""
        import os
        import subprocess
        import sys
        from unittest.mock import patch
        from hypothesis import settings
        from hypothesis.stateful import RuleBasedStateMachine, initialize, rule, run_state_machine_as_test
        from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
        from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
        def test_observed(tmp_path):
            environment = IsolatedApmEnvironment.create(tmp_path / "domain", base_env=os.environ)
            expected_command = (sys.executable, "-m", "apm_cli.cli", "--version")
            expected_cwd = environment.work_root
            expected_env = environment.subprocess_env()
            runner = ApmLifecycleRunner((sys.executable, "-m", "apm_cli.cli"))
            class Model(RuleBasedStateMachine):
                @initialize()
                def start(self):
                    result = runner.run(["--version"], cwd=environment.work_root,
                        env=environment.subprocess_env())
                    assert result.returncode == 0
                @rule()
                def step(self):
                    assert environment.home.is_dir()
            with patch("subprocess.Popen", wraps=subprocess.Popen) as popen:
                run_state_machine_as_test(Model, settings=settings(
                    max_examples=1, stateful_step_count=1, deadline=None, database=None))
            apm_calls = [
                call for call in popen.call_args_list
                if call.args and tuple(call.args[0]) == expected_command
            ]
            assert apm_calls
            for call in apm_calls:
                assert call.kwargs["cwd"] == expected_cwd
                assert call.kwargs["env"] == expected_env
    """)
    nodeid = f"{module.name}::test_observed"
    plugin = LifecycleEvidencePlugin([nodeid], None)
    result = pytester.runpytest_inprocess("-q", "-o", "addopts=", nodeid, plugins=[plugin])
    assert result.ret == 0
    record = plugin.records[nodeid]
    assert record["models"] == 1
    assert record["events"]
    assert all(event["model"] == 1 and event["returncode"] == 0 for event in record["events"])
    assert record["phases"] == {"setup": "passed", "call": "passed", "teardown": "passed"}
