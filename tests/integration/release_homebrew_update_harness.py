"""Structural release guard plus a hermetic model of the tap's local write."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

import yaml


def _assert_release_workflow_has_no_homebrew_push(workflow_path: Path) -> None:
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    for job_name, job in workflow.get("jobs", {}).items():
        rendered = json.dumps(job)
        if "homebrew" not in f"{job_name} {rendered}".lower():
            continue
        forbidden = ("GH_PKG_PAT", "repository-dispatch", "repository_dispatch", "gh pr create")
        found = [token for token in forbidden if token in rendered]
        if re.search(
            r"(?:secrets\.[A-Z0-9_]*(?:PAT|TOKEN)|github\.token)",
            rendered,
            re.IGNORECASE,
        ):
            found.append("workflow token")
        if found:
            raise RuntimeError(
                f"release workflow restores a Homebrew push/auth path: {', '.join(found)}"
            )


def _replace_assignment(formula: str, key: str, value: str) -> str:
    assignment = re.compile(rf'(?m)^  {re.escape(key)} "[^"]*"$')
    updated, count = assignment.subn(f'  {key} "{value}"', formula)
    if count != 1:
        raise RuntimeError(f"formula must contain exactly one {key} assignment")
    return updated


def update_tap(workflow_path: Path, release_path: Path, tap_repo: Path) -> None:
    """Model the tap's repository-local formula commit from release metadata."""
    _assert_release_workflow_has_no_homebrew_push(workflow_path)

    release = json.loads(release_path.read_text(encoding="utf-8"))
    formula_path = tap_repo / "Formula" / "apm.rb"
    formula = formula_path.read_text(encoding="utf-8")
    formula = _replace_assignment(formula, "version", release["tag_name"].removeprefix("v"))
    formula = _replace_assignment(formula, "url", release["asset_url"])
    formula = _replace_assignment(formula, "sha256", release["sha256"])
    formula_path.write_text(formula, encoding="utf-8")

    subprocess.run(["git", "add", str(formula_path)], cwd=tap_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"Update APM to {release['tag_name']}"],
        cwd=tap_repo,
        check=True,
        capture_output=True,
        text=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--tap-repo", type=Path, required=True)
    args = parser.parse_args()
    update_tap(args.workflow, args.release, args.tap_repo)


if __name__ == "__main__":
    main()
