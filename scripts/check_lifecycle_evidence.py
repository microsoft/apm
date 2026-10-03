#!/usr/bin/env python3
"""Execute #2867 lifecycle witnesses from this clean candidate, never replay proof.

Adapted mechanical provenance: be73ed139f3a73c19437a895dfd6d68e41df8bfc.
The optional completion is a dedicated native sidecar, not merge-worker output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lifecycle_contracts import (  # noqa: E402
    CONTRACT_ID,
    REQUIRED_WITNESSES,
    EvidenceError,
    candidate_contract,
    command_inventory,
    git,
    validate_execution,
)

LIMITATIONS = [
    "Source-Python execution is not packaged-binary or exhaustive platform parity.",
    "Assertions, applicability and model semantics require independent code-owner review.",
    "Candidate code and its verifier are not tamper-proof; review is the trust boundary.",
]


def _same_file(left: Path, right: Path) -> bool:
    """Include symlink and hardlink aliases when protecting driver evidence."""
    return left.resolve() == right.resolve() or (
        left.exists() and right.exists() and left.samefile(right)
    )


def source_profile(root: Path) -> tuple[Path | None, dict[str, Any]]:
    """Pin this checkout's source and the precise interpreter/launcher bytes."""
    import apm_cli
    from tests.utils.lifecycle_evidence import fingerprint

    source = root.resolve() / "src/apm_cli"
    if Path(apm_cli.__file__).resolve().parent != source:
        raise EvidenceError("Installed source does not resolve to the candidate checkout")
    launcher = Path(sys.executable).parent / "apm"
    executable = launcher.absolute() if os.name != "nt" and launcher.is_file() else None
    if executable:
        script = executable.read_text(encoding="utf-8")
        interpreter = Path(script.splitlines()[0][2:]) if script.startswith("#!") else None
        if script.startswith("#!/bin/sh\n"):
            trampoline = re.match(
                r"\A#!/bin/sh\n'''exec' "
                r"""(?:'([^'\r\n]+)'|"([^"$`\\\r\n]+)"|([A-Za-z0-9_./:+-]+))"""
                r""" "\$0" "\$@"\n' '''\n""",
                script,
            )
            interpreter = (
                Path(next(part for part in trampoline.groups() if part is not None))
                if trampoline
                else None
            )
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
        "source_root": str(source),
        "cli_sha256": fingerprint(source / "cli.py"),
        "executable": str(executable) if executable else None,
        "executable_sha256": fingerprint(executable) if executable else None,
        "python": str(Path(sys.executable).resolve()),
        "python_environment": str(Path(sys.executable).parent.resolve()),
        "python_sha256": fingerprint(Path(sys.executable).resolve()),
    }


def candidate(root: Path, base: str, head: str) -> dict[str, str]:
    """Require exact revisions, real ancestry and a clean tracked/untracked checkout."""
    if git(root, "for-each-ref", "--format=%(refname)", "refs/replace"):
        raise EvidenceError("Replacement refs are forbidden for lifecycle evidence")
    if os.environ.get("GIT_REPLACE_REF_BASE") or os.environ.get("GIT_GRAFTS_FILE"):
        raise EvidenceError("Replacement/graft environment is forbidden")
    resolved_base = git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    resolved_head = git(root, "rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}")
    if base != resolved_base or head != resolved_head:
        raise EvidenceError("Explicit full base/head commit SHAs are required")
    if resolved_head != git(root, "rev-parse", "HEAD"):
        raise EvidenceError("Requested head is not the checked-out candidate")
    if resolved_base == resolved_head:
        raise EvidenceError("Base and head must describe a candidate change")
    git(root, "merge-base", "--is-ancestor", resolved_base, resolved_head)
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise EvidenceError("Candidate must be clean, including untracked files")
    return {
        "base": resolved_base,
        "head": resolved_head,
        "tested_tree": git(root, "rev-parse", "HEAD^{tree}"),
    }


def _validate_completion_summary(summary: Any) -> None:
    """Use the same native schema for emission and independent consumption."""
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (ROOT / "tests/fixtures/lifecycle_completion.schema.json").read_text(encoding="utf-8")
    )
    errors = list(Draft202012Validator(schema).iter_errors(summary))
    if errors or type(summary.get("version")) is not int:
        raise EvidenceError(f"Malformed lifecycle completion schema: {errors[:1]}")
    if set(summary["witnesses"]) != set(REQUIRED_WITNESSES):
        raise EvidenceError("Completion requires exact immutable witnesses")


def _completion_input(path: Path, output: Path) -> tuple[dict[str, Any], Path]:
    """Validate the dedicated sidecar and refuse all output/input aliases."""
    if _same_file(output, path):
        raise EvidenceError("Independent report must not overwrite completion")
    summary = json.loads(path.read_text(encoding="utf-8"))
    _validate_completion_summary(summary)
    driver = Path(summary["report_path"])
    if not driver.is_absolute():
        driver = path.parent / driver
    if _same_file(output, driver):
        raise EvidenceError("Independent report must not overwrite driver evidence")
    if driver.resolve().is_relative_to(ROOT) or path.resolve().is_relative_to(ROOT):
        raise EvidenceError("Completion and driver report must be outside the checkout")
    return summary, driver


def _validate_source_profile(profile: Any) -> None:
    """Require recorded identities without resolving another checkout's paths."""
    if not isinstance(profile, dict) or profile.get("kind") != "source-python":
        raise EvidenceError("Native report requires a source-python profile")
    for field in ("source_root", "python", "python_environment", "executable"):
        if field == "executable" and field in profile and profile[field] is None:
            continue
        value = profile.get(field)
        if (
            not isinstance(value, str)
            or not value.strip()
            or not (
                Path(value).is_absolute()
                or PureWindowsPath(value).is_absolute()
                or PurePosixPath(value).is_absolute()
            )
        ):
            raise EvidenceError(f"Native source profile requires an absolute {field}")
    for field in ("cli_sha256", "python_sha256", "executable_sha256"):
        value = profile.get(field)
        if field == "executable_sha256" and profile["executable"] is None:
            valid = field in profile and value is None
        else:
            valid = isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
        if not valid:
            raise EvidenceError(f"Native source profile has invalid {field}")


def _validate_report_execution(report: dict[str, Any]) -> None:
    """Delegate report records to the current candidate's sole trajectory owner."""
    _validate_source_profile(report.get("profile"))
    records = report.get("witnesses")
    if not isinstance(records, dict) or set(records) != set(REQUIRED_WITNESSES):
        raise EvidenceError("Native report requires exact immutable witnesses")
    contract = candidate_contract(ROOT, report["base"], command_inventory())
    for witness in contract["witnesses"]:
        record = records[witness["nodeid"]]
        if not isinstance(record, dict):
            raise EvidenceError(f"{witness['nodeid']}: invalid execution record")
        validate_execution(witness, record)


def completion_summary(report: dict[str, Any], report_path: Path, raw: bytes) -> dict[str, Any]:
    """Derive success claims solely from the freshly executed report and its bytes."""
    if (
        report.get("status") != "passed"
        or report.get("lane") != "full"
        or json.loads(raw) != report
    ):
        raise EvidenceError("Completion emission requires the exact successful native report")
    _validate_report_execution(report)
    summary = {
        "version": report["version"],
        "contract_id": report["contract_id"],
        "base_sha": report["base"],
        "head_sha": report["head"],
        "tested_tree": report["tested_tree"],
        "lane": report["lane"],
        "status": report["status"],
        "report_path": str(report_path.resolve()),
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "witnesses": sorted(report["witnesses"]),
    }
    _validate_completion_summary(summary)
    return summary


def _json_bytes(value: dict[str, Any]) -> bytes:
    """Produce deterministic ASCII JSON with LF on every platform."""
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _write_new(path: Path, raw: bytes) -> None:
    """Never replace an existing receipt, even if it appeared after preflight."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)


def validate_completion(path: Path, native: dict[str, Any], output: Path) -> None:
    """Bind driver claims to independent fresh execution, allowing another source path."""
    summary, driver_path = _completion_input(path, output)
    fields = {
        "version": "version",
        "contract_id": "contract_id",
        "base_sha": "base",
        "head_sha": "head",
        "tested_tree": "tested_tree",
        "lane": "lane",
        "status": "status",
    }
    for claim, field in fields.items():
        if summary[claim] != native.get(field):
            raise EvidenceError(f"Completion disagrees with fresh execution: {claim}")
    raw = driver_path.read_bytes()
    if summary["report_sha256"] != hashlib.sha256(raw).hexdigest():
        raise EvidenceError("Completion driver report digest mismatch")
    driver = json.loads(raw)
    for report in (driver, native):
        if (
            not isinstance(report, dict)
            or type(report.get("version")) is not int
            or any(report.get(field) != native.get(field) for field in fields.values())
        ):
            raise EvidenceError("Driver report header/witnesses disagree with fresh execution")
        _validate_report_execution(report)
    if driver["profile"].get("cli_sha256") != native["profile"].get("cli_sha256"):
        raise EvidenceError("Driver source digest disagrees with fresh execution")


def execute(args: argparse.Namespace) -> dict[str, Any]:
    """Execute all witnesses now; no selected-contract or lane waiver exists."""
    identity = candidate(ROOT, args.base, args.head)
    report: dict[str, Any] = {
        "version": 1,
        "contract_id": CONTRACT_ID,
        **identity,
        "lane": args.lane,
        "status": "blocked",
        "profile": {"kind": "source-python"},
        "witnesses": {},
        "limitations": LIMITATIONS,
    }
    if args.lane != "full":
        raise EvidenceError("Only the full lifecycle lane is supported")
    plugin = None
    profile = None
    try:
        import pytest

        from tests.utils.lifecycle_evidence import LifecycleEvidencePlugin

        inventory = command_inventory()
        contract = candidate_contract(ROOT, identity["base"], inventory)
        report["command_inventory"] = sorted(inventory)
        executable, profile = source_profile(ROOT)
        report["profile"] = profile
        witnesses = contract["witnesses"]
        nodeids = sorted(REQUIRED_WITNESSES)
        contexts = {
            w["nodeid"]: {t.get("context", "initial") for t in w["transitions"]} for w in witnesses
        }
        plugin = LifecycleEvidencePlugin(
            nodeids, executable, contexts, Path(report["profile"]["source_root"])
        )
        # Never let pytest delete a pre-existing caller directory via --basetemp.
        scratch = args.report.absolute().parent / f".{args.report.name}.pytest"
        scratch.mkdir(parents=True, exist_ok=False)
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("APM_E2E_TESTS", "1")
            if executable:
                environment.setenv("APM_BINARY_PATH", str(executable))
            else:
                environment.delenv("APM_BINARY_PATH", raising=False)
            environment.delenv("PYTEST_ADDOPTS", raising=False)
            environment.setenv("HYPOTHESIS_STORAGE_DIRECTORY", str(scratch / "hypothesis"))
            exitcode = pytest.main(
                [
                    "-p",
                    "no:cacheprovider",
                    "-o",
                    "addopts=",
                    "--strict-markers",
                    "--basetemp",
                    str(scratch / "cases"),
                    "-q",
                    *nodeids,
                ],
                plugins=[plugin],
            )
        report["witnesses"] = plugin.records
        if exitcode != 0:
            raise EvidenceError(f"Lifecycle pytest execution failed with exit code {exitcode}")
        if set(plugin.records) != set(REQUIRED_WITNESSES):
            raise EvidenceError("Execution did not collect every immutable witness")
        for witness in witnesses:
            validate_execution(witness, plugin.records[witness["nodeid"]])
        report["status"] = "passed"
    except (EvidenceError, OSError, KeyError, TypeError, ValueError) as exc:
        report["error"] = str(exc)
    finally:
        if plugin is not None:
            plugin.patch.undo()
        try:
            if candidate(ROOT, args.base, args.head) != identity:
                raise EvidenceError("Candidate identity changed during execution")
        except (EvidenceError, OSError, subprocess.CalledProcessError) as exc:
            report["status"] = "blocked"
            report.setdefault("error", str(exc))
            report.setdefault("postflight_errors", []).append(str(exc))
        try:
            if profile is not None and source_profile(ROOT)[1] != profile:
                raise EvidenceError("Source executable identity changed during execution")
        except (EvidenceError, OSError, subprocess.CalledProcessError) as exc:
            report["status"] = "blocked"
            report.setdefault("error", str(exc))
            report.setdefault("postflight_errors", []).append(str(exc))
    return report


def main() -> int:
    """Write a new external report only, never clobber an existing receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--lane", choices=("full",), required=True)
    parser.add_argument("--report", type=Path, required=True)
    completion = parser.add_mutually_exclusive_group()
    completion.add_argument(
        "--completion", type=Path, help="Independently verify a driver sidecar with fresh execution"
    )
    completion.add_argument(
        "--completion-output",
        type=Path,
        help="Emit a native completion sidecar only after successful fresh execution",
    )
    args = parser.parse_args()
    if args.report.resolve().is_relative_to(ROOT):
        parser.error("--report must be outside the candidate checkout")
    if args.completion_output and args.completion_output.resolve().is_relative_to(ROOT):
        parser.error("--completion-output must be outside the candidate checkout")
    try:
        if args.report.exists() or args.report.is_symlink():
            raise EvidenceError("Report already exists; refusing to overwrite evidence")
        if args.completion_output:
            if _same_file(args.completion_output, args.report):
                raise EvidenceError("Report and completion output must be distinct")
            if args.completion_output.exists() or args.completion_output.is_symlink():
                raise EvidenceError(
                    "Completion output already exists; refusing to overwrite evidence"
                )
        if args.completion:
            _completion_input(args.completion, args.report)
        report = execute(args)
        if report["status"] != "passed":
            _write_new(args.report, _json_bytes(report))
            print(f"Lifecycle evidence: {report['status']} ({args.report})")
            raise EvidenceError(report.get("error", "Lifecycle execution did not pass"))
        if args.completion:
            validate_completion(args.completion, report, args.report)
        raw = _json_bytes(report)
        summary = (
            completion_summary(report, args.report, raw)
            if args.completion_output and report["status"] == "passed"
            else None
        )
        _write_new(args.report, raw)
        print(f"Lifecycle evidence: {report['status']} ({args.report})")
        if summary is not None:
            _write_new(args.completion_output, _json_bytes(summary))
            print(f"Lifecycle completion: {args.completion_output}")
    except (
        EvidenceError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"Lifecycle evidence blocked: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
