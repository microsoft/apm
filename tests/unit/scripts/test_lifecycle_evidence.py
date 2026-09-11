"""Fail-closed regressions for authored plans and fresh pytest observations."""

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

import click
import pytest

from scripts.check_lifecycle_evidence import (
    candidate,
    execute,
    main,
    source_profile,
    validate_completion,
)
from scripts.lifecycle_contracts import (
    EvidenceError,
    command_inventory,
    command_path,
    runtime_candidate,
    select_contracts,
    validate_contracts,
    validate_execution,
)
from tests.utils.lifecycle_evidence import LifecycleEvidencePlugin, snapshot

pytestmark = pytest.mark.component
pytest_plugins = ["pytester"]
RATCHET_TEST_SCOPE = "fixture"


def _ledger() -> dict[str, Any]:
    witnesses = [
        {
            "id": kind,
            "nodeid": f"tests/test_sample.py::test_{kind}[global]",
            "kind": kind,
            "dimensions": {"variant": "global"},
            "transitions": [
                {"command": "install", "argv_contains": [], "returncode": 0, "state": state}
                for state in ("changed", "unchanged")
            ],
        }
        for kind in ("deterministic", "generated")
    ]
    return {
        "schema_version": 1,
        "property_catalog": [{"id": "ownership.preserve_unowned"}],
        "lifecycle_contracts": [
            {
                "id": "feature",
                "assessment": "Shared global ownership affects install and repeated install.",
                "paths": ["src/new.py"],
                "properties": ["ownership.preserve_unowned"],
                "dimensions": {"variant": ["global"]},
                "commands": {
                    "install": {
                        "disposition": "applicable",
                        "reason": "Installs this feature.",
                        "deterministic": ["deterministic"],
                        "generated": ["generated"],
                    }
                },
                "witnesses": witnesses,
            }
        ],
    }


def _inventory() -> dict[str, click.Command]:
    return {"install": click.Command("install")}


def _launcher() -> Path | None:
    path = Path(sys.executable).parent / "apm"
    return path if path.is_file() else None


def _evidence() -> dict[str, Any]:
    return {
        "collected": True,
        "dimensions": {"variant": "global"},
        "phases": {"setup": "passed", "call": "passed", "teardown": "passed"},
        "models": 1,
        "events": [
            {
                "command": "install",
                "args": [],
                "returncode": 0,
                "cwd": "/fixture",
                "roots": {"HOME": "/fixture/home", "workspace": "/fixture"},
                "environment": {"HOME": "/fixture/home"},
                "model": 1,
                "before": before,
                "after": after,
            }
            for before, after in (("a", "b"), ("b", "b"))
        ],
    }


@pytest.mark.parametrize(
    "path",
    [
        "src/new.py",
        "src/renamed.py",
        "unknown/feature.md",
        "packages/skill/SKILL.md",
        "install.sh",
        "uv.lock",
        "pyproject.toml",
        "templates/new.yml",
        "scripts/new.py",
    ],
)
def test_unknown_and_payload_paths_conservatively_require_assessment(path: str) -> None:
    assert runtime_candidate(path)
    empty = {**_ledger(), "lifecycle_contracts": []}
    with pytest.raises(EvidenceError, match="Missing fresh lifecycle"):
        select_contracts(empty, empty, {path}, _inventory())


def test_fresh_exact_assessment_and_renames() -> None:
    head = _ledger()
    empty = {**head, "lifecycle_contracts": []}
    assert select_contracts(empty, head, {"src/new.py"}, _inventory())
    with pytest.raises(EvidenceError, match="Missing fresh lifecycle"):
        select_contracts(head, head, {"src/new.py"}, _inventory())
    with pytest.raises(EvidenceError, match=r"src/old.py"):
        select_contracts(empty, head, {"src/new.py", "src/old.py"}, _inventory())


def test_non_runtime_exception_is_exact_reasoned_and_not_reusable() -> None:
    base = {**_ledger(), "lifecycle_contracts": []}
    head = {**base, "non_runtime_changes": [{"paths": ["scripts/tool.py"], "reason": "CI tooling"}]}
    assert select_contracts(base, head, {"scripts/tool.py"}, _inventory()) == []
    with pytest.raises(EvidenceError, match="Missing fresh"):
        select_contracts(head, head, {"scripts/tool.py"}, _inventory())
    head["non_runtime_changes"][0]["paths"] = ["scripts/*"]
    with pytest.raises(EvidenceError, match="exact repository"):
        select_contracts(base, head, {"scripts/tool.py"}, _inventory())


@pytest.mark.parametrize("mutation", ["contract", "witness", "dimension", "command", "path"])
def test_coordinated_obligation_removal_fails(mutation: str) -> None:
    base = _ledger()
    head = copy.deepcopy(base)
    row = head["lifecycle_contracts"][0]
    if mutation == "contract":
        head["lifecycle_contracts"] = []
    elif mutation == "witness":
        row["witnesses"][0]["nodeid"] = "tests/test_other.py::test_other"
    elif mutation == "dimension":
        row["dimensions"] = {}
    elif mutation == "command":
        row["commands"]["install"] = {"disposition": "semantic_na", "reason": "Removed"}
    else:
        row["paths"] = ["src/replacement.py"]
    with pytest.raises(EvidenceError):
        select_contracts(base, head, set(), _inventory())


@pytest.mark.parametrize("mutation", ["inventory", "kind", "dimension", "transition", "property"])
def test_incomplete_contract_fails(mutation: str) -> None:
    ledger = _ledger()
    row = ledger["lifecycle_contracts"][0]
    if mutation == "inventory":
        row["commands"] = {}
    elif mutation == "kind":
        row["witnesses"][1]["kind"] = "deterministic"
    elif mutation == "dimension":
        row["dimensions"]["variant"].append("aliased")
    elif mutation == "transition":
        row["witnesses"][0]["transitions"] = []
    else:
        row["properties"] = ["invented.property"]
    with pytest.raises(EvidenceError):
        validate_contracts(ledger, _inventory())


@pytest.mark.parametrize(
    "mutation",
    [
        "uncollected",
        "skip",
        "xfail",
        "setup",
        "call",
        "teardown",
        "dimension",
        "model",
        "missing-transition",
        "status",
        "workspace",
        "state",
        "json-only",
        "outside-model",
        "roots",
        "continuity",
        "model-splice",
        "environment",
    ],
)
def test_execution_obligations_fail_closed(mutation: str) -> None:
    witness = _ledger()["lifecycle_contracts"][0]["witnesses"][1]
    evidence = _evidence()
    validate_execution(witness, evidence)
    if mutation == "uncollected":
        evidence["collected"] = False
    elif mutation in {"setup", "call", "teardown"}:
        evidence["phases"][mutation] = "failed"
    elif mutation in {"skip", "xfail"}:
        evidence["phases"]["call"] = mutation
    elif mutation == "dimension":
        evidence["dimensions"] = {}
    elif mutation == "model":
        evidence["models"] = 0
    elif mutation == "missing-transition":
        evidence["events"].pop()
    elif mutation == "status":
        evidence["events"][1]["returncode"] = 1
    elif mutation == "workspace":
        evidence["events"][1]["cwd"] = "/other"
    elif mutation == "state":
        evidence["events"][1]["after"] = "different"
    elif mutation == "outside-model":
        evidence["events"][1]["model"] = None
    elif mutation == "roots":
        evidence["events"][1]["roots"]["HOME"] = "/other/home"
    elif mutation == "continuity":
        evidence["events"][1].update(before="different", after="different")
    elif mutation == "model-splice":
        evidence["models"] = 2
        evidence["events"][1]["model"] = 2
    elif mutation == "environment":
        del evidence["events"][1]["environment"]
    else:
        evidence = {"status": "passed"}
    with pytest.raises(EvidenceError):
        validate_execution(witness, evidence)


def test_intentional_fixture_mutation_requires_reviewed_preparation() -> None:
    witness = _ledger()["lifecycle_contracts"][0]["witnesses"][1]
    evidence = _evidence()
    evidence["events"][1].update(before="tampered", after="tampered")
    with pytest.raises(EvidenceError, match="trajectory"):
        validate_execution(witness, evidence)
    witness["transitions"][1]["preparation"] = "User tampers with deployed bytes before audit."
    validate_execution(witness, evidence)


def test_reviewed_apm_home_context_retains_one_trajectory() -> None:
    witness = _ledger()["lifecycle_contracts"][0]["witnesses"][1]
    evidence = _evidence()
    for event in evidence["events"]:
        event["roots"]["APM_HOME"] = "/fixture/home/.apm"
        event["environment"]["APM_HOME"] = "/fixture/home/.apm"
    evidence["events"][1].update(cwd="/fixture/home/.apm", context="APM_HOME")
    with pytest.raises(EvidenceError, match="trajectory"):
        validate_execution(witness, evidence)
    witness["transitions"][1]["context"] = "APM_HOME"
    validate_execution(witness, evidence)
    evidence["events"][1]["cwd"] = "/fixture/unrelated"
    with pytest.raises(EvidenceError, match="outside its observed context"):
        validate_execution(witness, evidence)


def test_domain_keeps_snapshot_roots_stable_across_physical_apm_home(
    tmp_path: Path,
) -> None:
    caller = tmp_path / "caller"
    home = tmp_path / "home"
    caller.mkdir()
    (home / ".apm").mkdir(parents=True)
    env = {
        "HOME": str(home),
        "APM_HOME": str(home / ".apm"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        "HERMES_HOME": str(tmp_path / "hermes"),
    }
    plugin = LifecycleEvidencePlugin(["test"], None, {"test": {"APM_HOME"}})
    plugin.active = "test"
    initial, _, first_context = plugin._domain(caller, env)
    before = snapshot(initial)
    global_roots, _, context = plugin._domain(home / ".apm", env)
    assert first_context == "initial" and context == "APM_HOME"
    assert global_roots == initial
    assert snapshot(global_roots) == before
    for key in ("CLAUDE_CONFIG_DIR", "HERMES_HOME"):
        root = Path(env[key])
        root.mkdir()
        (root / "sentinel").write_text("external target bytes", encoding="ascii")
        assert snapshot(global_roots) != before
        before = snapshot(global_roots)
    with pytest.raises(EvidenceError, match="Unreviewed"):
        plugin._domain(tmp_path / "unrelated", env)
    for key in ("HOME", "APM_HOME"):
        with pytest.raises(EvidenceError, match="durable roots changed"):
            plugin._domain(caller, {**env, key: str(tmp_path / "other")})
    undeclared = LifecycleEvidencePlugin(["test"], None)
    undeclared.active = "test"
    undeclared._domain(caller, env)
    with pytest.raises(EvidenceError, match="Undeclared"):
        undeclared._domain(home / ".apm", env)


def test_domain_does_not_splice_distinct_hypothesis_instances(tmp_path: Path) -> None:
    plugin = LifecycleEvidencePlugin(["test"], None)
    plugin.active, plugin.model_run = "test", 1
    first, _, _ = plugin._domain(tmp_path / "first/caller", {"HOME": str(tmp_path / "first/home")})
    second, _, _ = plugin._domain(
        tmp_path / "second/caller", {"HOME": str(tmp_path / "second/home")}
    )
    assert first != second
    witness = _ledger()["lifecycle_contracts"][0]["witnesses"][1]
    evidence = _evidence()
    for event, roots in zip(evidence["events"], (first, second), strict=True):
        event.update(roots=roots, cwd=roots["workspace"])
    with pytest.raises(EvidenceError, match="trajectory"):
        validate_execution(witness, evidence)


@pytest.mark.parametrize("override", ["../outside-hermes", " ~/external-hermes "])
@pytest.mark.parametrize(
    ("key", "resolver"),
    [
        ("HERMES_HOME", "resolve_hermes_root()"),
        ("CLAUDE_CONFIG_DIR", "Path.home() / KNOWN_TARGETS['claude'].for_scope(True).root_dir"),
        ("APM_CACHE_DIR", "get_cache_root()"),
        ("XDG_CACHE_HOME", "get_cache_root().parent"),
    ],
)
def test_domain_observes_actual_child_resolved_root(
    tmp_path: Path, override: str, key: str, resolver: str
) -> None:
    from tests.utils.isolated_apm_environment import DURABLE_ENVIRONMENT_ROOTS

    caller = tmp_path / "caller"
    caller.mkdir()
    env = {
        **{
            name: value
            for name, value in os.environ.items()
            if name not in DURABLE_ENVIRONMENT_ROOTS
        },
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        key: override,
    }
    plugin = LifecycleEvidencePlugin(["test"], None)
    plugin.active = "test"
    roots, environment, _ = plugin._domain(caller, env)
    before = snapshot(roots)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from apm_cli.integration.targets import KNOWN_TARGETS, resolve_hermes_root; "
            "from apm_cli.cache.paths import get_cache_root; "
            f"root = ({resolver}).resolve(); root.mkdir(parents=True, exist_ok=True); "
            "(root / 'sentinel').write_text('child wrote here'); print(root)",
        ],
        cwd=caller,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert environment[key] == override
    assert roots[key] == result.stdout.strip()
    assert snapshot(roots) != before
    assert plugin._domain(caller, env)[0] == roots


def test_relative_roots_cannot_move_with_reviewed_cwd(tmp_path: Path) -> None:
    caller = tmp_path / "caller"
    apm_home = tmp_path / "home/apm"
    caller.mkdir()
    apm_home.mkdir(parents=True)
    env = {"APM_HOME": str(apm_home), "HERMES_HOME": "../hermes"}
    plugin = LifecycleEvidencePlugin(["test"], None, {"test": {"APM_HOME"}})
    plugin.active = "test"
    plugin._domain(caller, env)
    with pytest.raises(EvidenceError, match="durable roots changed"):
        plugin._domain(apm_home, env)


@pytest.mark.parametrize("output", ["not json", "[]", "{}", '{"HERMES_HOME": null}'])
def test_root_expansion_rejects_malformed_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    from tests.utils import lifecycle_evidence as observer

    monkeypatch.setattr(
        observer.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output),
    )
    with pytest.raises(EvidenceError, match="Child durable root expansion"):
        observer._physical_roots(tmp_path, {"HERMES_HOME": "~/hermes"})


def test_child_source_probe_rejects_old_checkout_even_when_cli_would_pass(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old"
    package = old / "apm_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="ascii")
    (package / "cli.py").write_text(
        "if __name__ == '__main__':\n    print('expected-success')\n", encoding="ascii"
    )
    env = {**os.environ, "PYTHONPATH": str(old), "HOME": str(tmp_path / "home")}
    result = subprocess.run(
        [sys.executable, "-m", "apm_cli.cli", "--version"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and result.stdout.strip() == "expected-success"
    plugin = LifecycleEvidencePlugin(["test"], None)
    assert plugin.candidate_source != package
    with pytest.raises(EvidenceError, match="Child source identity"):
        plugin._source_identity(tmp_path, env, 10)
    good = {**env, "PYTHONPATH": str(plugin.candidate_source.parent)}
    assert plugin._source_identity(tmp_path, good, 10)["package"] == str(plugin.candidate_source)
    with pytest.raises(EvidenceError, match="Child source identity"):
        plugin._source_identity(tmp_path, env, 10)


@pytest.mark.parametrize("has_contract", [False, True])
def test_smoke_deferral_leaves_proof_to_fresh_native_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_contract: bool
) -> None:
    from scripts import check_lifecycle_evidence as gate
    from tests.utils import lifecycle_evidence as observer

    contract = _ledger()["lifecycle_contracts"][0]
    deferred = contract["witnesses"][0]
    removed = []
    config = SimpleNamespace(
        rootpath=tmp_path,
        getoption=lambda name: "base" if name.endswith("base") else "head",
        hook=SimpleNamespace(pytest_deselected=lambda items: removed.extend(items)),
    )
    monkeypatch.setattr(gate, "candidate", lambda *args: {"base": "base", "head": "head"})
    monkeypatch.setattr(
        observer, "candidate_contracts", lambda *args: (set(), [contract] if has_contract else [])
    )
    items = [SimpleNamespace(nodeid=w["nodeid"]) for w in contract["witnesses"]]
    items.append(SimpleNamespace(nodeid="tests/test_unrelated.py::test_other"))
    observer.pytest_collection_modifyitems(config, items)
    assert len(removed) == int(has_contract)
    assert any("unrelated" in item.nodeid for item in items)
    assert any("test_generated" in item.nodeid for item in items)
    if has_contract:
        assert removed[0].nodeid == deferred["nodeid"]
        with pytest.raises(EvidenceError, match="not collected"):
            validate_execution(deferred, {})
        failed = _evidence()
        failed["phases"]["call"] = "failed"
        with pytest.raises(EvidenceError, match="must pass"):
            validate_execution(deferred, failed)


def test_smoke_deferral_rejects_stale_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import check_lifecycle_evidence as gate
    from tests.utils import lifecycle_evidence as observer

    config = SimpleNamespace(rootpath=tmp_path, getoption=lambda _name: "stale")

    def reject(*args: object) -> None:
        raise EvidenceError("Requested head is not the checked-out candidate")

    monkeypatch.setattr(gate, "candidate", reject)
    with pytest.raises(pytest.UsageError, match="checked-out candidate"):
        observer.pytest_collection_modifyitems(config, [])


def test_smoke_plugin_loads_before_collection_from_repository() -> None:
    from scripts.check_lifecycle_evidence import ROOT

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.utils.lifecycle_evidence",
            "--help",
        ],
        cwd=ROOT,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--lifecycle-defer-base" in result.stdout
    assert "--lifecycle-defer-head" in result.stdout


def test_smoke_defers_exact_node_before_real_loadgroup_decoration(
    pytester: pytest.Pytester,
) -> None:
    from scripts.check_lifecycle_evidence import ROOT

    pytester.makeconftest(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        pytest_plugins = ["tests.utils.lifecycle_evidence"]
        def pytest_configure(config):
            from scripts import check_lifecycle_evidence as gate
            from tests.utils import lifecycle_evidence as observer
            gate.candidate = lambda *args: {{"base": "base", "head": "head"}}
            observer.candidate_contracts = lambda *args: (set(), [{{
                "witnesses": [{{
                    "nodeid": "test_grouped.py::test_deferred[global]",
                    "kind": "deterministic",
                }}],
            }}])
    """)
    pytester.makepyfile(
        test_grouped="""
        import pytest
        pytestmark = pytest.mark.xdist_group("home_env")
        @pytest.mark.parametrize("variant", ["global"])
        def test_deferred(variant):
            pytest.fail("must execute only in the subsequent native gate")
        def test_retained(request):
            assert request.node.nodeid.endswith("@home_env")
    """
    )
    result = pytester.runpytest_subprocess(
        "-q",
        "-n",
        "2",
        "--dist",
        "loadgroup",
        "--lifecycle-defer-base",
        "base",
        "--lifecycle-defer-head",
        "head",
    )
    result.assert_outcomes(passed=1)
    assert result.ret == 0


@pytest.mark.skipif(os.name == "nt", reason="Installed Unix launcher import-path control")
def test_source_probe_distinguishes_launcher_and_module_search_paths(tmp_path: Path) -> None:
    package = tmp_path / "apm_cli"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="ascii")
    (package / "cli.py").write_text(
        "if __name__ == '__main__':\n    print('shadow-cli')\n", encoding="ascii"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONSAFEPATH"}
    }
    plugin = LifecycleEvidencePlugin(["test"], _launcher())
    assert plugin.executable is not None
    launched = subprocess.run(
        [str(plugin.executable), "--version"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert launched.returncode == 0 and "shadow-cli" not in launched.stdout
    assert plugin._source_identity(tmp_path, env, 10, (str(plugin.executable),))["package"] == str(
        plugin.candidate_source
    )
    with pytest.raises(EvidenceError, match="Child source identity"):
        plugin._source_identity(tmp_path, env, 10, (sys.executable, "-m", "apm_cli.cli"))


def test_inventory_uses_recursive_registrations_and_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apm_cli.cli import cli

    monkeypatch.setitem(cli.commands, "fixture-alias", cli.commands["install"])
    inventory = command_inventory()
    assert inventory["fixture-alias"] is inventory["install"]
    assert "marketplace package add" in inventory
    assert "" in inventory and "lock" in inventory and "lock export" in inventory
    assert command_path(["--verbose", "cache", "prune", "--help"], inventory) == "cache prune"
    assert command_path(["lock"], inventory) == "lock"
    assert command_path(["--help"], inventory) == ""


def test_candidate_rejects_stale_head_and_dirty_tree(tmp_path: Path) -> None:
    def run(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    run("init", "-q")
    run("config", "user.email", "fixture@example.invalid")
    run("config", "user.name", "Fixture")
    run("commit", "--allow-empty", "-qm", "base")
    base = run("rev-parse", "HEAD")
    run("commit", "--allow-empty", "-qm", "head")
    head = run("rev-parse", "HEAD")
    assert candidate(tmp_path, base, head)["tested_tree"] == run("rev-parse", "HEAD^{tree}")
    with pytest.raises(EvidenceError, match="checked-out"):
        candidate(tmp_path, base, base)
    (tmp_path / "unknown").write_text("dirty")
    with pytest.raises(EvidenceError, match="clean"):
        candidate(tmp_path, base, head)


def test_candidate_requires_history_beyond_fetched_shallow_endpoints(tmp_path: Path) -> None:
    origin, checkout = tmp_path / "origin", tmp_path / "checkout"
    origin.mkdir()

    def run(root: Path, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    run(origin, "init", "-q")
    run(origin, "config", "user.email", "fixture@example.invalid")
    run(origin, "config", "user.name", "Fixture")
    run(origin, "commit", "--allow-empty", "-qm", "base")
    base = run(origin, "rev-parse", "HEAD")
    run(origin, "commit", "--allow-empty", "-qm", "head")
    head = run(origin, "rev-parse", "HEAD")
    run(tmp_path, "clone", "-q", "--depth=1", origin.as_uri(), str(checkout))
    run(checkout, "fetch", "-q", "origin", base, head)
    with pytest.raises(subprocess.CalledProcessError):
        candidate(checkout, base, head)
    run(checkout, "fetch", "-q", "--unshallow", "origin")
    assert candidate(checkout, base, head)["head"] == head


def test_plugin_observes_actual_execution_and_rejects_deselection(
    pytester: pytest.Pytester,
) -> None:
    module = pytester.makepyfile("""
        import os
        import sys
        import pytest
        from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
        from hypothesis import settings
        from hypothesis.stateful import RuleBasedStateMachine, rule, run_state_machine_as_test
        @pytest.mark.parametrize("variant", ["global"])
        def test_real(tmp_path, variant):
            env = {
                **os.environ, "HOME": str(tmp_path / "home"),
                "HERMES_HOME": "../external-hermes",
            }
            runner = ApmLifecycleRunner((sys.executable, "-m", "apm_cli.cli"))
            for _ in range(2):
                result = runner.run(["--version"], cwd=tmp_path, env=env)
                assert result.returncode == 0
        def test_skip():
            pytest.skip("contract skip")
        @pytest.mark.xfail(reason="contract xfail")
        def test_xfail():
            assert False
        @pytest.mark.parametrize("variant", ["global"])
        def test_generated(tmp_path, variant):
            class Model(RuleBasedStateMachine):
                @rule()
                def version(self):
                    env = {**os.environ, "HOME": str(tmp_path / "home")}
                    runner = ApmLifecycleRunner((sys.executable, "-m", "apm_cli.cli"))
                    for _ in range(2):
                        assert runner.run(["--version"], cwd=tmp_path, env=env).returncode == 0
            run_state_machine_as_test(
                Model, settings=settings(
                    max_examples=1, stateful_step_count=1, deadline=None, database=None
                )
            )
    """)
    path = module.name
    real = f"{path}::test_real[global]"
    skipped, xfailed = f"{path}::test_skip", f"{path}::test_xfail"
    generated = f"{path}::test_generated[global]"
    executable = _launcher()
    plugin = LifecycleEvidencePlugin([real, skipped, xfailed, generated], executable)
    result = pytester.runpytest_inprocess("-q", "-o", "addopts=", plugins=[plugin])
    result.assert_outcomes(passed=2, skipped=1, xfailed=1)
    record = plugin.records[real]
    assert record["dimensions"] == {"variant": "global"}
    assert len(record["events"]) == 2
    for event in record["events"]:
        assert event["environment"]["HERMES_HOME"] == "../external-hermes"
        assert event["roots"]["HERMES_HOME"] == str(
            (Path(event["cwd"]).parent / "external-hermes").resolve()
        )
    assert record["phases"] == {"setup": "passed", "call": "passed", "teardown": "passed"}
    assert plugin.records[skipped]["phases"]["call"] == "skipped"
    assert plugin.records[xfailed]["phases"]["call"] == "xfail"
    assert plugin.records[generated]["models"] == 1
    assert len(plugin.records[generated]["events"]) >= 2
    assert all(event["model"] == 1 for event in plugin.records[generated]["events"])
    deselected = LifecycleEvidencePlugin([real], executable)
    result = pytester.runpytest_inprocess("-q", "-k", "skip", plugins=[deselected])
    assert real not in deselected.records
    assert result.ret == 0
    assert json.dumps(record)


@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_plugin_records_fixture_failures(pytester: pytest.Pytester, phase: str) -> None:
    module = pytester.makepyfile(f"""
        import pytest
        @pytest.fixture
        def broken():
            if {phase!r} == "setup":
                raise RuntimeError("fixture failed")
            yield
            raise RuntimeError("fixture failed")
        def test_fixture(broken):
            assert True
    """)
    nodeid = f"{module.name}::test_fixture"
    plugin = LifecycleEvidencePlugin([nodeid], _launcher())
    result = pytester.runpytest_inprocess("-q", plugins=[plugin])
    assert result.ret != 0
    assert plugin.records[nodeid]["phases"][phase] == "failed"


@pytest.mark.parametrize("selector", ["::WrongClass::test_exists", "::test_exists[missing]"])
def test_plugin_rejects_wrong_class_or_parameter(pytester: pytest.Pytester, selector: str) -> None:
    module = pytester.makepyfile("""
        import pytest
        @pytest.mark.parametrize("variant", ["actual"])
        def test_exists(variant):
            assert variant
    """)
    nodeid = f"{module.name}{selector}"
    plugin = LifecycleEvidencePlugin([nodeid], _launcher())
    result = pytester.runpytest_inprocess("-q", nodeid, plugins=[plugin])
    assert result.ret != 0
    assert not plugin.records


@pytest.mark.skipif(os.name == "nt", reason="Unix script launcher; Windows uses the source module")
def test_source_profile_rejects_foreign_source_and_stale_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apm_cli

    monkeypatch.setattr(apm_cli, "__file__", str(tmp_path / "src/apm_cli/__init__.py"))
    python = tmp_path / "python"
    python.write_text("fixture interpreter", encoding="ascii")
    monkeypatch.setattr(sys, "executable", str(python))
    assert source_profile(tmp_path)[0] is None
    launcher = tmp_path / "apm"
    launcher.write_text("#!/foreign/python\nfrom apm_cli.cli import cli\n", encoding="ascii")
    with pytest.raises(EvidenceError, match="this Python environment"):
        source_profile(tmp_path)
    launcher.write_text(f"#!{python}\nfrom apm_cli.cli import cli\n", encoding="ascii")
    _, initial = source_profile(tmp_path)
    launcher.write_text(f"#!{python}\nfrom apm_cli.cli import cli\n# changed\n", encoding="ascii")
    assert source_profile(tmp_path)[1] != initial
    with pytest.raises(EvidenceError, match="candidate checkout"):
        source_profile(tmp_path / "different")


@pytest.mark.skipif(os.name == "nt", reason="Unix launcher interpreter aliases")
def test_source_profile_accepts_only_same_environment_interpreter_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import venv

    from scripts.check_lifecycle_evidence import ROOT

    for name in ("current", "foreign"):
        venv.EnvBuilder(with_pip=False, symlinks=True).create(tmp_path / name)
    current = tmp_path / "current/bin"
    foreign = tmp_path / "foreign/bin"
    monkeypatch.setattr(sys, "executable", str(current / "python3"))
    launcher = current / "apm"
    launcher.write_text(f"#!{current / 'python'}\nfrom apm_cli.cli import cli\n", encoding="ascii")
    assert source_profile(ROOT)[0] == launcher
    assert (current / "python").samefile(foreign / "python")
    launcher.write_text(f"#!{foreign / 'python'}\nfrom apm_cli.cli import cli\n", encoding="ascii")
    with pytest.raises(EvidenceError, match="this Python environment"):
        source_profile(ROOT)


def test_cli_rejects_in_tree_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.check_lifecycle_evidence import ROOT

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_lifecycle_evidence.py",
            "--base",
            "a",
            "--head",
            "b",
            "--lane",
            "full",
            "--report",
            str(ROOT / "receipt.json"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def _completion(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    native = {
        "version": 1,
        "base": "a" * 40,
        "head": "b" * 40,
        "tested_tree": "c" * 40,
        "lane": "full",
        "status": "passed",
    }
    driver = tmp_path / "driver.json"
    driver.write_text(json.dumps(native), encoding="ascii")
    data = {
        "head_sha": native["head"],
        "lifecycle_evidence": {
            "version": 1,
            "base_sha": native["base"],
            "head_sha": native["head"],
            "tested_tree": native["tested_tree"],
            "lane": "full",
            "status": "passed",
            "report_path": str(driver),
            "report_sha256": hashlib.sha256(driver.read_bytes()).hexdigest(),
        },
    }
    completion = tmp_path / "completion.json"
    completion.write_text(json.dumps(data), encoding="ascii")
    return completion, data, native


@pytest.mark.parametrize(
    "mutation",
    [
        "base_sha",
        "head_sha",
        "tested_tree",
        "lane",
        "status",
        "version",
        "report_sha256",
        "report_path",
        "top_head",
        "header",
        "malformed",
        "missing",
    ],
)
def test_completion_rejects_stale_or_malformed_claims(tmp_path: Path, mutation: str) -> None:
    path, data, native = _completion(tmp_path)
    output = tmp_path / "independent.json"
    validate_completion(path, native, output)
    summary = data["lifecycle_evidence"]
    if mutation in {"base_sha", "head_sha", "tested_tree", "report_sha256"}:
        summary[mutation] = "d" * len(summary[mutation])
    elif mutation == "lane":
        summary["lane"] = "pr"
    elif mutation == "status":
        summary["status"] = "pending"
    elif mutation == "version":
        summary["version"] = True
    elif mutation == "report_path":
        summary["report_path"] = ""
    elif mutation == "top_head":
        data["head_sha"] = "d" * 40
    elif mutation == "header":
        driver = Path(summary["report_path"])
        driver.write_text(json.dumps({**native, "tested_tree": "d" * 40}), encoding="ascii")
        summary["report_sha256"] = hashlib.sha256(driver.read_bytes()).hexdigest()
    elif mutation == "malformed":
        data["lifecycle_evidence"] = []
    else:
        del data["lifecycle_evidence"]
    path.write_text(json.dumps(data), encoding="ascii")
    with pytest.raises((EvidenceError, KeyError, TypeError)):
        validate_completion(path, native, output)


def test_completion_accepts_resolved_summary_and_protects_driver_bytes(tmp_path: Path) -> None:
    path, data, native = _completion(tmp_path)
    del data["head_sha"]
    data["lifecycle_evidence"]["report_path"] = "driver.json"
    path.write_text(json.dumps(data), encoding="ascii")
    validate_completion(path, native, tmp_path / "independent.json")
    for output in (path, tmp_path / "driver.json"):
        before = output.read_bytes()
        with pytest.raises(EvidenceError, match="overwrite"):
            validate_completion(path, native, output)
        assert output.read_bytes() == before
    hardlink = tmp_path / "driver-hardlink.json"
    hardlink.hardlink_to(tmp_path / "driver.json")
    with pytest.raises(EvidenceError, match="overwrite"):
        validate_completion(path, native, hardlink)


def test_completion_always_executes_native_gate_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import check_lifecycle_evidence as gate

    completion, _data, native = _completion(tmp_path)
    calls = []

    def fresh(args: argparse.Namespace) -> dict[str, Any]:
        calls.append(args.lane)
        return dict(native)

    monkeypatch.setattr(gate, "execute", fresh)
    output = tmp_path / "independent.json"
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
            str(completion),
        ],
    )
    assert main() == 0
    assert calls == ["full"]
    completion.write_text("not json", encoding="ascii")
    assert main() == 1
    assert calls == ["full", "full"]
    assert json.loads(output.read_text())["status"] == "blocked"
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
            "pr",
            "--report",
            str(output),
            "--completion",
            str(completion),
        ],
    )
    with pytest.raises(SystemExit):
        main()
    assert calls == ["full", "full"]


def test_completion_preserves_driver_even_when_claims_are_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import check_lifecycle_evidence as gate

    completion, data, native = _completion(tmp_path)
    driver = Path(data["lifecycle_evidence"]["report_path"])
    data["lifecycle_evidence"]["status"] = "pending"
    completion.write_text(json.dumps(data), encoding="ascii")
    original = driver.read_bytes()
    monkeypatch.setattr(gate, "execute", lambda _args: dict(native))
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
            str(driver),
            "--completion",
            str(completion),
        ],
    )
    assert main() == 1
    assert driver.read_bytes() == original


@pytest.mark.parametrize("lane,status", [("pr", "pending"), ("full", "passed")])
def test_execute_produces_candidate_bound_native_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str, status: str
) -> None:
    """Exercise the gate, git comparison, real collection, subprocesses and model together."""
    from scripts import check_lifecycle_evidence as gate

    profile = source_profile(gate.ROOT)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "source_profile", lambda _root: profile)
    monkeypatch.chdir(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    filename = f"test_native_probe_{lane}.py"
    (tests / filename).write_text(
        """
import os
import sys
import pytest
from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, rule, run_state_machine_as_test
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
@pytest.mark.parametrize("kind", ["deterministic", "generated"])
def test_probe(tmp_path, kind):
    def commands():
        env = {**os.environ, "HOME": str(tmp_path / "home"), "USERPROFILE": str(tmp_path / "home")}
        runner = ApmLifecycleRunner((sys.executable, "-m", "apm_cli.cli"))
        for _ in range(2):
            assert runner.run(["--version"], cwd=tmp_path, env=env).returncode == 0
    if kind == "deterministic":
        commands()
    else:
        class Model(RuleBasedStateMachine):
            @rule()
            def version(self):
                commands()
        run_state_machine_as_test(Model, settings=settings(
            max_examples=1, stateful_step_count=1, database=None, deadline=None))
""",
        encoding="ascii",
    )
    ledger = _ledger()
    path = tests / "fixtures/lifecycle_bug_ledger.json"
    path.parent.mkdir()
    path.write_text(json.dumps({**ledger, "lifecycle_contracts": []}), encoding="ascii")

    def commit(message: str) -> str:
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-qm", message], check=True)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], check=True)
    (tmp_path / ".gitignore").write_text("__pycache__/\n.hypothesis/\n", encoding="ascii")
    base = commit("base")
    contract = ledger["lifecycle_contracts"][0]
    contract["dimensions"] = {}
    contract["commands"] = {
        name: {"disposition": "semantic_na", "reason": "Fixture only tests version reporting."}
        for name in command_inventory()
    }
    contract["commands"][""] = {
        "disposition": "applicable",
        "reason": "Version reporting fixture.",
        "deterministic": ["deterministic"],
        "generated": ["generated"],
    }
    for witness in contract["witnesses"]:
        witness["nodeid"] = f"tests/{filename}::test_probe[{witness['kind']}]"
        witness["dimensions"] = {"kind": witness["kind"]}
        for transition in witness["transitions"]:
            transition.update(command="", argv_contains=["--version"], state="observed")
    path.write_text(json.dumps(ledger), encoding="ascii")
    head = commit("contract")
    report = execute(argparse.Namespace(base=base, head=head, lane=lane))
    assert report["status"] == status, report
    assert report["base"] == base and report["head"] == head
    assert len(report["witnesses"]) == (2 if lane == "full" else 1)
    assert all(value["events"] for value in report["witnesses"].values())
