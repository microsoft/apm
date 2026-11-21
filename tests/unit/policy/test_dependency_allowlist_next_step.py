"""A failed ``dependency-allowlist`` audit check carries one runnable next step (#4570).

The advice comes from the remediation catalog: ``apm uninstall`` naming the
apm.yml entries that bring in every denied dependency, or asking the org admin
to allow them, naming the policy source. Text and SARIF output render it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apm_cli.commands.audit import _render_ci_failures_to_stderr
from apm_cli.core.remediation import catalog
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.policy.models import CheckResult, CIAuditResult
from apm_cli.policy.policy_checks import run_policy_checks
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-remediation-advice")]

_POLICY = ApmPolicy(dependencies=DependencyPolicy(allow=["github.com/contoso/**"]))
_SOURCE = "org:contoso/.github"


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _project(root: Path, deps: list[str], locked: list[LockedDependency] = ()) -> Path:
    (root / "apm.yml").write_text(
        yaml.safe_dump({"name": "demo", "version": "1.0.0", "dependencies": {"apm": deps}}),
        encoding="utf-8",
    )
    if locked:
        lock = LockFile()
        for dep in locked:
            lock.add_dependency(dep)
        lock.write(root / "apm.lock.yaml")
    return root


def _allowlist(root: Path) -> CheckResult:
    result = run_policy_checks(root, _POLICY, fail_fast=False, policy_source=_SOURCE)
    return next(c for c in result.checks if c.name == "dependency-allowlist")


def test_catalog_entry_runs_apm_uninstall() -> None:
    entry = catalog()["dependency-not-allowed"]

    assert entry.command == "apm uninstall {packages}"
    assert entry.fixes_promise == "remove {subject} outside the org allow list"


def test_one_denied_dependency_names_it_and_the_policy_source(tmp_path: Path) -> None:
    check = _allowlist(_project(tmp_path, ["acme/evil-pkg"]))

    assert not check.passed
    assert check.next_step == (
        "Run 'apm uninstall acme/evil-pkg' to remove the dependency outside the org "
        "allow list, or ask your org admin to allow it (policy source: org:contoso/.github)."
    )
    assert [r.to_json() for r in check.remediations] == [
        {
            "id": "dependency-not-allowed",
            "command": "apm uninstall acme/evil-pkg",
            "fixes": "remove the dependency outside the org allow list",
        }
    ]


def test_several_denied_dependencies_name_each_entry_that_brings_them_in(
    tmp_path: Path,
) -> None:
    locked = [
        LockedDependency(repo_url="acme/evil-pkg", depth=1),
        LockedDependency(repo_url="acme/other-pkg", depth=1),
        LockedDependency(repo_url="contoso/parent", depth=1),
        LockedDependency(repo_url="acme/hidden", depth=2, resolved_by="contoso/parent"),
    ]
    root = _project(tmp_path, ["acme/evil-pkg", "acme/other-pkg", "contoso/parent"], locked)

    check = _allowlist(root)

    assert len(check.subjects) == 3
    assert check.next_step == (
        "Run 'apm uninstall acme/evil-pkg acme/other-pkg contoso/parent' to remove the 3 "
        "dependencies outside the org allow list, or ask your org admin to allow them "
        "(policy source: org:contoso/.github)."
    )
    assert check.remediations[0].argv() == (
        "apm",
        "uninstall",
        "acme/evil-pkg",
        "acme/other-pkg",
        "contoso/parent",
    )


def test_text_and_sarif_render_the_next_step(tmp_path: Path, capsys) -> None:
    check = _allowlist(_project(tmp_path, ["acme/evil-pkg"]))
    result = CIAuditResult(checks=[check])

    _render_ci_failures_to_stderr(result)
    stderr = capsys.readouterr().err.splitlines()
    sarif = result.to_sarif()["runs"][0]["results"]

    assert stderr[:3] == [
        "[x] dependency-allowlist: 1 dependency(ies) not in allow list",
        "    - github.com/acme/evil-pkg: not in allowed sources",
        f"    {check.next_step}",
    ]
    assert [r["message"]["text"] for r in sarif] == [
        f"github.com/acme/evil-pkg: not in allowed sources. {check.next_step}"
    ]


def test_other_checks_keep_their_sarif_message() -> None:
    check = CheckResult(name="lockfile-exists", passed=False, message="m", details=["d"])

    sarif = CIAuditResult(checks=[check]).to_sarif()["runs"][0]["results"]

    assert [r["message"]["text"] for r in sarif] == ["d"]
