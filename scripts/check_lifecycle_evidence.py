#!/usr/bin/env python3
"""Execute lifecycle obligations for an exact candidate; never import receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# Direct script execution must resolve first-party helpers from this checkout.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lifecycle_contracts import (  # noqa: E402
    EvidenceError,
    candidate_contracts,
    command_inventory,
    git,
    validate_execution,
)

LIMITATIONS = [
    "Applicability, assertions and model semantics require code-owner review.",
    "PR code and its verifier are not tamper-proof; protected review is the trust boundary.",
    "Bounded source-Python trajectories do not establish packaged or exhaustive parity.",
    "A pr-lane pending report is not shipping evidence; execute lane full independently.",
]


class CompletionOutputConflict(EvidenceError):
    """The proposed independent report would overwrite the driver's inputs."""


def _same_file(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve() or (
        left.exists() and right.exists() and left.samefile(right)
    )


def source_profile(root: Path) -> tuple[Path | None, dict[str, Any]]:
    """Identify the installed editable source and its exact interpreter/launcher."""
    import apm_cli
    from tests.utils.lifecycle_evidence import fingerprint

    if Path(apm_cli.__file__).resolve().parent != root / "src/apm_cli":
        raise EvidenceError("Installed source does not resolve to the candidate checkout")
    launcher = Path(sys.executable).parent / "apm"
    executable = launcher if os.name != "nt" and launcher.is_file() else None
    if executable:
        script = executable.read_text(encoding="utf-8")
        interpreter = Path(script.splitlines()[0][2:]) if script.startswith("#!") else None
        if (
            "apm_cli.cli" not in script
            or interpreter is None
            or not interpreter.is_absolute()
            or not interpreter.is_file()
            or not interpreter.samefile(sys.executable)
            or interpreter.parent.resolve() != Path(sys.executable).parent.resolve()
        ):
            raise EvidenceError("Installed apm entrypoint is not this Python environment")
    return executable, {
        "kind": "source-python",
        "source_root": str(root / "src/apm_cli"),
        "executable": str(executable) if executable else None,
        "executable_sha256": fingerprint(executable) if executable else None,
        "python": str(Path(sys.executable).resolve()),
        "python_sha256": fingerprint(Path(sys.executable).resolve()),
    }


def candidate(root: Path, base: str, head: str) -> dict[str, str]:
    """Require explicit existing revisions and a pristine tested source tree."""
    resolved_base = git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    resolved_head = git(root, "rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}")
    if resolved_head != git(root, "rev-parse", "HEAD"):
        raise EvidenceError("Requested head is not the checked-out candidate")
    if resolved_base == resolved_head:
        raise EvidenceError("Base and head must describe a candidate change")
    git(root, "merge-base", "--is-ancestor", resolved_base, resolved_head)
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise EvidenceError("Candidate must be clean, including untracked files")
    return {
        "base": resolved_base,
        "head": resolved_head,
        "tested_tree": git(root, "rev-parse", "HEAD^{tree}"),
    }


def validate_completion(path: Path, native: dict[str, Any], output: Path) -> None:
    """Compare a driver's summary to independent execution, never use it as proof."""
    if _same_file(output, path):
        raise CompletionOutputConflict("Independent report must not overwrite completion")
    completion = json.loads(path.read_text(encoding="utf-8"))
    summary = completion["lifecycle_evidence"]
    if not isinstance(summary, dict):
        raise EvidenceError("Malformed lifecycle completion summary")
    report_path = summary.get("report_path")
    if not isinstance(report_path, str) or not report_path.strip():
        raise EvidenceError("Completion requires its driver's report_path")
    driver_path = Path(report_path)
    if not driver_path.is_absolute():
        driver_path = path.parent / driver_path
    if _same_file(output, driver_path):
        raise CompletionOutputConflict("Independent report must not overwrite driver evidence")
    if type(summary.get("version")) is not int:
        raise EvidenceError("Malformed lifecycle completion version")
    if summary["version"] != 1 or summary.get("lane") != "full":
        raise EvidenceError("Completion requires version 1 full-lane evidence")
    if summary.get("status") not in {"passed", "not_applicable"}:
        raise EvidenceError("Completion requires passed or not_applicable evidence")
    fields = {
        "version": "version",
        "base_sha": "base",
        "head_sha": "head",
        "tested_tree": "tested_tree",
        "lane": "lane",
        "status": "status",
    }
    for claim, field in fields.items():
        if summary.get(claim) != native.get(field):
            raise EvidenceError(f"Completion disagrees with fresh execution: {claim}")
    if "head_sha" in completion and completion["head_sha"] != native["head"]:
        raise EvidenceError("Completion top-level head_sha is stale")
    raw = driver_path.read_bytes()
    if summary.get("report_sha256") != hashlib.sha256(raw).hexdigest():
        raise EvidenceError("Completion driver report digest mismatch")
    driver = json.loads(raw)
    if (
        not isinstance(driver, dict)
        or type(driver.get("version")) is not int
        or any(driver.get(field) != native.get(field) for field in fields.values())
    ):
        raise EvidenceError("Completion driver report header disagrees with fresh execution")


def execute(args: argparse.Namespace) -> dict[str, Any]:
    """Generate native evidence from this process's own fresh pytest execution."""
    identity = candidate(ROOT, args.base, args.head)
    report: dict[str, Any] = {
        "version": 1,
        **identity,
        "lane": args.lane,
        "status": "blocked",
        "profile": {"kind": "source-python"},
        "contracts": [],
        "witnesses": {},
        "limitations": LIMITATIONS,
    }
    try:
        inventory = command_inventory()
        changed, contracts = candidate_contracts(
            ROOT, identity["base"], identity["head"], inventory
        )
        report["contracts"] = [contract["id"] for contract in contracts]
        report["command_inventory"] = sorted(inventory)
        report["changed_paths"] = sorted(changed)
        if not contracts:
            if candidate(ROOT, args.base, args.head) != identity:
                raise EvidenceError("Candidate identity changed during assessment")
            report["status"] = "not_applicable"
            return report
        witnesses = [
            witness
            for contract in contracts
            for witness in contract["witnesses"]
            if args.lane == "full" or witness["kind"] == "deterministic"
        ]
        if not witnesses:
            raise EvidenceError("Empty lifecycle witness selection")

        import pytest

        from tests.utils.lifecycle_evidence import LifecycleEvidencePlugin

        executable, report["profile"] = source_profile(ROOT)
        nodeids = sorted({witness["nodeid"] for witness in witnesses})
        contexts = {
            nodeid: {
                transition.get("context", "initial")
                for witness in witnesses
                if witness["nodeid"] == nodeid
                for transition in witness["transitions"]
            }
            for nodeid in nodeids
        }
        plugin = LifecycleEvidencePlugin(
            nodeids, executable, contexts, Path(report["profile"]["source_root"])
        )
        if executable:
            os.environ["APM_BINARY_PATH"] = str(executable)
        else:
            os.environ.pop("APM_BINARY_PATH", None)
        os.environ["APM_E2E_TESTS"] = "1"
        # No user-provided pytest options, deselection filters or receipt inputs.
        os.environ.pop("PYTEST_ADDOPTS", None)
        exitcode = pytest.main(
            ["-p", "no:cacheprovider", "-o", "addopts=", "--strict-markers", "-q", *nodeids],
            plugins=[plugin],
        )
        report["witnesses"] = plugin.records
        if exitcode != 0:
            raise EvidenceError(f"Lifecycle pytest execution failed with exit code {exitcode}")
        for witness in witnesses:
            validate_execution(witness, plugin.records.get(witness["nodeid"], {}))
        if candidate(ROOT, args.base, args.head) != identity:
            raise EvidenceError("Candidate identity changed during execution")
        if source_profile(ROOT)[1] != report["profile"]:
            raise EvidenceError("Source executable identity changed during execution")
        report["status"] = "passed" if args.lane == "full" else "pending"
    except (EvidenceError, KeyError, TypeError, ValueError) as exc:
        report["error"] = str(exc)
    return report


def main() -> int:
    """CLI entrypoint; nonzero means blocked, pending is explicitly lane-scoped."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--lane", choices=("pr", "full"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--completion", type=Path)
    args = parser.parse_args()
    if args.report.resolve().is_relative_to(ROOT):
        parser.error("--report must be outside the candidate checkout")
    if args.completion and args.lane != "full":
        parser.error("--completion requires --lane full")
    try:
        report = execute(args)
        if args.completion:
            try:
                validate_completion(args.completion, report, args.report)
            except CompletionOutputConflict as exc:
                print(f"Completion verification failed: {exc}")
                return 1
            except (OSError, KeyError, TypeError, ValueError) as exc:
                report["status"] = "blocked"
                report["error"] = (
                    f"{report.get('error', '')} Completion verification failed: {exc}"
                ).strip()
    except (EvidenceError, subprocess.CalledProcessError) as exc:
        report = {
            "version": 1,
            "base": args.base,
            "head": args.head,
            "tested_tree": None,
            "lane": args.lane,
            "status": "blocked",
            "error": str(exc),
            "witnesses": {},
            "limitations": LIMITATIONS,
        }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="ascii")
    print(f"Lifecycle evidence: {report['status']} ({args.report})")
    return 1 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
