"""Static dual guard for bounded native lifecycle proof, never a native campaign."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RULE = "contracts-tooling-native-lifecycle-evidence"
PROVIDER = "scripts/check_lifecycle_evidence.py"
CONTRACTS = "scripts/lifecycle_contracts.py"
OBSERVER = "tests/utils/lifecycle_evidence.py"
RUNNER = "tests/utils/apm_lifecycle_runner.py"
REGISTRY = ".apm/architecture/owners/contracts-tooling.json"


def test_native_evidence_has_one_registered_owner_and_bounded_guard() -> None:
    registry = json.loads((ROOT / REGISTRY).read_text(encoding="ascii"))
    owners = {entry["id"]: entry for entry in registry["owners"]}
    native = owners["native-lifecycle-evidence"]
    rules = [rule for rule in registered_rules() if RULE in rule.guard_ids]
    assert len(rules) == 1
    assert rules[0].id == RULE
    assert native["guards"] == [RULE]
    assert {PROVIDER, CONTRACTS, OBSERVER} <= set(native["selectors"])
    assert (
        "src/apm_cli/commands/deps/cli.py"
        in owners["dependency-identity-materialization"]["selectors"]
    )
    assert not any(path.startswith("src/") for path in native["selectors"])
    report = run_selected_rules(ROOT, (RULE,))
    assert report.failures == ()
    assert report.violations == ()


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (RUNNER, "subprocess.Popen(", "fabricated_process("),
        (RUNNER, "returncode=process.returncode", "returncode=0"),
        (PROVIDER, '"refs/replace"', '"refs/unrelated"'),
        (
            PROVIDER,
            'resolved_head != git(root, "rev-parse", "HEAD")',
            "resolved_head != resolved_head",
        ),
        (PROVIDER, '"--untracked-files=all"', '"--untracked-files=no"'),
        (PROVIDER, '"HEAD^{tree}"', '"HEAD"'),
        (PROVIDER, "Path(apm_cli.__file__).resolve().parent != source", "False"),
        (PROVIDER, "interpreter.samefile(sys.executable)", "True"),
        (PROVIDER, 'fingerprint(source / "cli.py")', '"unverified"'),
        (
            PROVIDER,
            "identity = candidate(ROOT, args.base, args.head)",
            "identity = cached_candidate()",
        ),
        (PROVIDER, "if candidate(ROOT, args.base, args.head) != identity:", "if False:"),
        (
            PROVIDER,
            "if profile is not None and source_profile(ROOT)[1] != profile:",
            "if False:",
        ),
        (PROVIDER, "profile is not None and source_profile", "source_profile"),
        (
            PROVIDER,
            'report.setdefault("error", str(exc))',
            'report["error"] = str(exc)',
        ),
        (
            PROVIDER,
            "finally:\n        if plugin is not None:",
            "else:\n        if plugin is not None:",
        ),
        (PROVIDER, "pytest.main(", "cached_report("),
        (PROVIDER, "plugins=[plugin]", "plugins=[]"),
        (
            PROVIDER,
            'validate_execution(witness, plugin.records[witness["nodeid"]])',
            "accept_record(witness)",
        ),
        (PROVIDER, "nodeids = sorted(REQUIRED_WITNESSES)", "nodeids = sorted([])"),
        (PROVIDER, "report = execute(args)", "report = load_saved_report(args)"),
        (PROVIDER, "Draft202012Validator(schema).iter_errors(summary)", "iter(())"),
        (
            PROVIDER,
            "validate_completion(args.completion, report, args.report)",
            "accept_completion()",
        ),
        (PROVIDER, "completion_summary(report, args.report, raw)", "authored_summary()"),
        (
            PROVIDER,
            "_validate_completion_summary(summary)\n    driver",
            "accept_summary(summary)\n    driver",
        ),
        (PROVIDER, 'summary["report_sha256"] != hashlib.sha256(raw).hexdigest()', "False"),
        (PROVIDER, '"tested_tree": "tested_tree"', '"tested_tree": "head"'),
        (PROVIDER, "summary[claim] != native.get(field)", "False"),
        (
            PROVIDER,
            'candidate_contract(ROOT, report["base"], command_inventory())',
            "cached_contract()",
        ),
        (PROVIDER, "validate_execution(witness, record)", "accept_record(witness, record)"),
        (
            PROVIDER,
            '_validate_source_profile(report.get("profile"))',
            "accept_profile(report)",
        ),
        (
            PROVIDER,
            '("source_root", "python", "python_environment", "executable")',
            '("source_root",)',
        ),
        (
            PROVIDER,
            '("cli_sha256", "python_sha256", "executable_sha256")',
            '("cli_sha256",)',
        ),
        (PROVIDER, 're.fullmatch(r"[0-9a-f]{64}", value)', "True"),
        (PROVIDER, "valid = field in profile and value is None", "valid = True"),
        (
            PROVIDER,
            "_validate_report_execution(report)\n    summary",
            "accept_report(report)\n    summary",
        ),
        (
            PROVIDER,
            "        _validate_report_execution(report)\n    if driver",
            "        accept_report(report)\n    if driver",
        ),
        (
            PROVIDER,
            'if report["status"] != "passed":',
            'if report["status"] == "unused":',
        ),
        (
            PROVIDER,
            "_write_new(args.report, _json_bytes(report))",
            "discard_failed_report(report)",
        ),
        (CONTRACTS, "REQUIRED_WITNESSES = (", "REMOVED_REQUIRED_WITNESSES = ("),
        (CONTRACTS, "== set(REQUIRED_WITNESSES)", "== set()"),
        (CONTRACTS, "validate_contracts(current, inventory)", "unvalidated_contracts(current)"),
        (
            OBSERVER,
            "original = ApmLifecycleRunner._run_with_timeout",
            "original = fabricated_result",
        ),
        (OBSERVER, 'self.patch.setattr(ApmLifecycleRunner, "_run_with_timeout", observe)', "pass"),
        (OBSERVER, "result = original(runner, args, **kwargs)", "result = fabricated_result()"),
        (OBSERVER, "before = snapshot(roots)", 'before = "constant"'),
        (OBSERVER, '"after": snapshot(roots)', '"after": "constant"'),
        (
            OBSERVER,
            'self._source_identity(cwd, env, kwargs["timeout_seconds"], command)',
            "cached_source()",
        ),
        (OBSERVER, "probe = subprocess.run(", "probe = cached_probe("),
        (OBSERVER, "if source != expected:", "if False:"),
        (OBSERVER, "model = stateful.run_state_machine_as_test", "model = fabricated_model"),
        (OBSERVER, "result = model(*args, **kwargs)", "result = None"),
        (OBSERVER, "set(actual) != self.nodeids", "False"),
        (OBSERVER, 'hasattr(report, "wasxfail")', "False"),
    ],
)
def test_native_evidence_guard_rejects_proof_bypass(path: str, old: str, new: str) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    if old == "probe = subprocess.run(":
        prefix, suffix = source.rsplit(old, 1)
        mutated = prefix + new + suffix
    else:
        mutated = source.replace(old, new, 1)
    assert mutated != source
    report = run_selected_rules(ROOT, (RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(item.rule_id == RULE and item.path == path for item in report.violations)


@pytest.mark.parametrize("path", [PROVIDER, CONTRACTS, OBSERVER])
def test_native_evidence_guard_rejects_missing_and_invalid_owner_source(path: str) -> None:
    for mutation in ("", "def broken("):
        report = run_selected_rules(ROOT, (RULE,), source_overrides={path: mutation})
        if mutation:
            assert report.failures
            assert all(item.stage == f"parse:{path}" for item in report.failures)
        else:
            assert report.failures == ()
        assert any(item.rule_id == RULE and item.path == path for item in report.violations)
