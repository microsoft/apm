"""A failed ``dependency-allowlist`` audit check carries one runnable next step (#4570).

The advice comes from the remediation catalog: ``apm uninstall`` naming the
apm.yml entries whose removal provably drops every denied dependency, or
asking the org admin to allow them, naming the policy source. When the
uninstall cannot be proven to clear the failure the advice is admin-only.
Text and SARIF output render it.
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

_SOURCE = "org:contoso/.github"
_ADMIN_ONLY = "    Ask your org admin to allow them (policy source: org:contoso/.github)."


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _locked(key: str, parent: str | None = None) -> LockedDependency:
    return LockedDependency(repo_url=key, depth=2 if parent else 1, resolved_by=parent)


def _project(root: Path, deps: list[str], locked: list[LockedDependency], children=None) -> Path:
    """Write apm.yml, the lock and each locked package's own manifest under apm_modules/."""
    children = children or {}
    (root / "apm.yml").write_text(
        yaml.safe_dump({"name": "demo", "version": "1.0.0", "dependencies": {"apm": deps}}),
        encoding="utf-8",
    )
    lock = LockFile()
    for dep in locked:
        lock.add_dependency(dep)
        manifest = {"name": dep.repo_url.rsplit("/", 1)[-1], "version": "1.0.0"}
        if dep.repo_url in children:
            manifest["dependencies"] = {"apm": children[dep.repo_url]}
        package = root / "apm_modules" / dep.repo_url
        package.mkdir(parents=True)
        (package / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    lock.write(root / "apm.lock.yaml")
    return root


def _allowlist(root: Path, *, require: tuple[str, ...] = ()) -> CheckResult:
    policy = ApmPolicy(
        dependencies=DependencyPolicy(allow=["github.com/contoso/**"], require=list(require))
    )
    result = run_policy_checks(root, policy, fail_fast=False, policy_source=_SOURCE)
    return next(c for c in result.checks if c.name == "dependency-allowlist")


def _stderr(capsys, *checks: CheckResult) -> list[str]:
    _render_ci_failures_to_stderr(CIAuditResult(checks=list(checks)))
    return capsys.readouterr().err.splitlines()


def _several(root: Path, deps: list[str], *extra: LockedDependency, children=None) -> Path:
    locked = [
        _locked("acme/evil-pkg"),
        _locked("acme/other-pkg"),
        _locked("contoso/parent"),
        _locked("acme/hidden", parent="contoso/parent"),
        *extra,
    ]
    return _project(root, deps, locked, {"contoso/parent": ["acme/hidden"], **(children or {})})


def test_one_denied_dependency_names_it_and_the_policy_source(tmp_path: Path, capsys) -> None:
    check = _allowlist(_project(tmp_path, ["acme/evil-pkg"], [_locked("acme/evil-pkg")]))

    assert catalog()["dependency-not-allowed"].command == "apm uninstall {packages}"
    assert _stderr(capsys, check)[2] == (
        "    Run 'apm uninstall acme/evil-pkg' to remove the dependency outside the org "
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
    tmp_path: Path, capsys
) -> None:
    root = _several(tmp_path, ["acme/evil-pkg", "acme/other-pkg", "contoso/parent"])

    stderr = _stderr(capsys, _allowlist(root))

    assert stderr[0] == "[x] dependency-allowlist: 3 dependency(ies) not in allow list"
    assert stderr[4] == (
        "    Run 'apm uninstall acme/evil-pkg acme/other-pkg contoso/parent' to remove the 3 "
        "dependencies outside the org allow list, or ask your org admin to allow them "
        "(policy source: org:contoso/.github)."
    )


@pytest.mark.parametrize(
    ("deps", "extra", "children", "require"),
    [
        pytest.param(
            ["acme/evil-pkg", "acme/other-pkg", "contoso/parent"],
            (),
            {},
            ("contoso/parent",),
            id="removal-drops-a-required-package",
        ),
        pytest.param(
            ["acme/evil-pkg", "acme/other-pkg", "contoso/parent", "contoso/sibling"],
            (_locked("contoso/sibling"),),
            {"contoso/sibling": ["acme/hidden"]},
            (),
            id="a-surviving-package-still-reaches-a-denied-one",
        ),
        pytest.param(
            ["acme/evil-pkg", "acme/other-pkg", "contoso/parent", "acme/hidden"],
            (),
            {},
            (),
            id="a-declared-denied-package-the-lock-still-records-as-transitive",
        ),
    ],
)
def test_unproven_uninstall_falls_back_to_the_admin(
    tmp_path: Path, capsys, deps, extra, children, require
) -> None:
    check = _allowlist(_several(tmp_path, deps, *extra, children=children), require=require)

    assert _stderr(capsys, check)[4] == _ADMIN_ONLY
    assert check.remediations == ()


def test_sarif_carries_the_next_step_and_other_checks_are_unchanged(tmp_path: Path, capsys) -> None:
    check = _allowlist(_project(tmp_path, ["acme/evil-pkg"], [_locked("acme/evil-pkg")]))
    other = CheckResult(name="lockfile-exists", passed=False, message="m", details=["d"])
    result = CIAuditResult(checks=[check, other])

    sarif = result.to_sarif()["runs"][0]["results"]
    stderr = _stderr(capsys, check, other)

    assert [r["message"]["text"] for r in sarif] == [
        f"github.com/acme/evil-pkg: not in allowed sources. {check.next_step}",
        "d",
    ]
    assert stderr[3:6] == ["[x] lockfile-exists: m", "    - d", "[x] 2 of 2 check(s) failed"]


def test_an_edited_deployed_file_uninstall_would_refuse_falls_back_to_the_admin(
    tmp_path: Path, capsys
) -> None:
    deployed = ".github/instructions/evil.instructions.md"
    root = _project(
        tmp_path,
        ["acme/evil-pkg"],
        [
            LockedDependency(
                repo_url="acme/evil-pkg",
                depth=1,
                deployed_files=[deployed],
                deployed_file_hashes={deployed: "sha256:" + "0" * 64},
            )
        ],
    )
    (root / deployed).parent.mkdir(parents=True)
    (root / deployed).write_text("# edited by the user\n", encoding="utf-8")

    check = _allowlist(root)

    assert _stderr(capsys, check)[2] == (
        "    Ask your org admin to allow it (policy source: org:contoso/.github)."
    )
    assert check.remediations == ()
