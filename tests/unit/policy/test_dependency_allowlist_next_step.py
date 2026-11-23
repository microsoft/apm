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
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy, ManifestPolicy

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-remediation-advice")]

_SOURCE = "org:contoso/.github"
_REPLACE_SCHEMA = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)
_ADMIN_ONLY = "    Ask your org admin to allow them (policy source: org:contoso/.github)."


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """The download boundary fails locally: evidence replays never reach the network."""

    def _no_network(*_args, **_kwargs):
        raise RuntimeError("offline: package downloads are disabled in this test")

    monkeypatch.setattr(
        "apm_cli.deps.github_downloader.GitHubPackageDownloader.download_package", _no_network
    )


def _locked(key: str, parent: str | None = None) -> LockedDependency:
    return LockedDependency(repo_url=key, depth=2 if parent else 1, resolved_by=parent)


def _project(
    root: Path, deps: list[str], locked: list[LockedDependency], children=None, **manifest
) -> Path:
    """Write apm.yml, the lock and each locked package's own manifest under apm_modules/."""
    children = children or {}
    (root / "apm.yml").write_text(
        yaml.safe_dump(
            {"name": "demo", "version": "1.0.0", "dependencies": {"apm": deps}, **manifest}
        ),
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


def _allowlist(
    root: Path,
    *,
    require: tuple[str, ...] = (),
    modules_root: Path | None = None,
    required_fields: tuple[str, ...] = (),
) -> CheckResult:
    policy = ApmPolicy(
        enforcement="block",
        dependencies=DependencyPolicy(allow=["github.com/contoso/**"], require=list(require)),
        manifest=ManifestPolicy(required_fields=required_fields),
    )
    result = run_policy_checks(
        root, policy, fail_fast=False, modules_root=modules_root, policy_source=_SOURCE
    )
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


def test_sarif_and_text_emit_the_next_step_once_per_check(tmp_path: Path, capsys) -> None:
    root = _several(tmp_path, ["acme/evil-pkg", "acme/other-pkg", "contoso/parent"])
    check = _allowlist(root)

    sarif = CIAuditResult(checks=[check]).to_sarif()["runs"][0]["results"]
    texts = [r["message"]["text"] for r in sarif]
    stderr = _stderr(capsys, check)

    assert len(texts) == len(check.details) == 3
    assert sum(check.next_step in text for text in texts) == 1
    assert texts[-1].endswith(f". {check.next_step}")
    assert sum(check.next_step in line for line in stderr) == 1


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


def test_removing_a_replaced_required_package_falls_back_to_the_admin(
    tmp_path: Path, capsys
) -> None:
    replace = [
        {
            "for": {"git": "https://github.com/contoso/original"},
            "with": {"git": "https://github.com/contoso/fork", "ref": "v1.0.0"},
        }
    ]
    root = _project(
        tmp_path,
        ["contoso/original"],
        [_locked("contoso/original"), _locked("acme/hidden", parent="contoso/original")],
        {"contoso/original": ["acme/hidden"]},
        **{"$schema": _REPLACE_SCHEMA, "replace": replace},
    )

    check = _allowlist(root, require=("contoso/fork",))

    assert _stderr(capsys, check)[2] == (
        "    Ask your org admin to allow it (policy source: org:contoso/.github)."
    )


def test_without_a_live_apm_modules_the_replay_tree_proves_nothing(tmp_path: Path, capsys) -> None:
    root = _several(tmp_path, ["acme/evil-pkg", "acme/other-pkg", "contoso/parent"])
    replay = tmp_path.parent / f"{tmp_path.name}-replay"
    (root / "apm_modules").rename(replay)

    check = _allowlist(root, modules_root=replay)

    assert _stderr(capsys, check)[4] == _ADMIN_ONLY
    assert check.remediations == ()


def test_a_survivor_shrink_that_retires_an_edited_skill_falls_back_to_the_admin(
    tmp_path: Path, capsys
) -> None:
    skills = [".agents/skills/alpha", ".agents/skills/beta"]
    files = [f"{skill}/SKILL.md" for skill in skills]
    bundle = LockedDependency(
        repo_url="contoso/bundle",
        depth=2,
        resolved_by="acme/evil-pkg",
        skill_subset=["alpha", "beta"],
        deployed_files=[*skills, *files],
        deployed_file_hashes={path: "sha256:" + "0" * 64 for path in files},
    )
    root = _project(
        tmp_path,
        ["acme/evil-pkg", "contoso/keep"],
        [_locked("acme/evil-pkg"), _locked("contoso/keep"), bundle],
        {
            "acme/evil-pkg": [{"git": "contoso/bundle", "skills": ["beta"]}],
            "contoso/keep": [{"git": "contoso/bundle", "skills": ["alpha"]}],
        },
    )
    for path in files:
        (root / path).parent.mkdir(parents=True)
        (root / path).write_text("# edited by the user\n", encoding="utf-8")

    check = _allowlist(root)

    assert _stderr(capsys, check)[2] == (
        "    Ask your org admin to allow it (policy source: org:contoso/.github)."
    )
    assert check.remediations == ()


def test_a_policy_uninstall_cannot_resolve_falls_back_to_the_admin(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = _project(
        tmp_path,
        ["acme/evil-pkg", "contoso/keep"],
        [_locked("acme/evil-pkg"), _locked("contoso/keep")],
        policy={"fetch_failure_default": "block"},
    )

    def _outage(*_args, **_kwargs):
        raise OSError("HTTP 503 fetching policy from contoso/.github")

    # The network boundary: the policy repository is down for this run.
    monkeypatch.setattr("apm_cli.policy.discovery.discover_policy_with_chain", _outage)

    check = _allowlist(root)

    assert _stderr(capsys, check)[2] == (
        "    Ask your org admin to allow it (policy source: org:contoso/.github)."
    )
    assert check.remediations == ()


def test_the_advice_never_migrates_the_users_executable_consent(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    home = tmp_path / "apm-home"
    home.mkdir()
    legacy = home / "approvals.yml"
    legacy.write_text("contoso/keep:\n  hook: true\n", encoding="utf-8")
    config = home / "config.json"
    monkeypatch.setenv("APM_HOME", str(home))
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(config))
    project = tmp_path / "project"
    project.mkdir()
    root = _project(
        project,
        ["acme/evil-pkg", "contoso/keep"],
        [_locked("acme/evil-pkg"), _locked("contoso/keep")],
    )

    check = _allowlist(root)

    assert _stderr(capsys, check)[2].startswith("    Run 'apm uninstall acme/evil-pkg'")
    assert legacy.read_text(encoding="utf-8") == "contoso/keep:\n  hook: true\n"
    assert not config.exists()


def test_an_unmanaged_recorded_path_uninstall_would_refuse_falls_back_to_the_admin(
    tmp_path: Path, capsys
) -> None:
    root = _project(
        tmp_path,
        ["acme/evil-pkg"],
        [LockedDependency(repo_url="acme/evil-pkg", depth=1, deployed_files=["notes.txt"])],
    )

    check = _allowlist(root)

    assert _stderr(capsys, check)[2] == (
        "    Ask your org admin to allow it (policy source: org:contoso/.github)."
    )
    assert check.remediations == ()


def test_a_symlinked_lockfile_uninstall_would_refuse_falls_back_to_the_admin(
    tmp_path: Path, capsys
) -> None:
    root = _project(tmp_path, ["acme/evil-pkg"], [_locked("acme/evil-pkg")])
    real = tmp_path / "locks" / "apm.lock.yaml"
    real.parent.mkdir()
    (root / "apm.lock.yaml").rename(real)
    (root / "apm.lock.yaml").symlink_to(real)

    check = _allowlist(root)

    assert _stderr(capsys, check)[2] == (
        "    Ask your org admin to allow it (policy source: org:contoso/.github)."
    )
    assert check.remediations == ()


def test_a_checkout_outside_apm_modules_falls_back_to_the_admin(tmp_path: Path, capsys) -> None:
    (tmp_path / "project").mkdir()
    root = _project(tmp_path / "project", ["acme/evil-pkg"], [_locked("acme/evil-pkg")])
    checkout = root / "apm_modules" / "acme" / "evil-pkg"
    outside = tmp_path / "elsewhere"
    checkout.rename(outside)
    checkout.symlink_to(outside, target_is_directory=True)

    check = _allowlist(root)

    assert _stderr(capsys, check)[2] == (
        "    Ask your org admin to allow it (policy source: org:contoso/.github)."
    )
    assert check.remediations == ()


def test_a_surviving_dev_dependency_that_reaches_the_denied_one_falls_back_to_the_admin(
    tmp_path: Path, capsys
) -> None:
    root = _several(
        tmp_path,
        ["acme/evil-pkg", "acme/other-pkg", "contoso/parent"],
        _locked("contoso/dev-tool"),
        children={"contoso/dev-tool": ["acme/hidden"]},
    )
    manifest = yaml.safe_load((root / "apm.yml").read_text(encoding="utf-8"))
    manifest["devDependencies"] = {"apm": ["contoso/dev-tool"]}
    (root / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    check = _allowlist(root)

    assert _stderr(capsys, check)[4] == _ADMIN_ONLY
    assert check.remediations == ()


def test_a_null_dev_dependencies_section_yields_advice_that_clears_the_failure(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from click.testing import CliRunner

    from apm_cli.cli import cli

    root = _project(
        tmp_path,
        ["acme/evil-pkg", "contoso/keep"],
        [_locked("acme/evil-pkg"), _locked("contoso/keep")],
        devDependencies=None,
    )

    check = _allowlist(root)

    assert _stderr(capsys, check)[2].startswith("    Run 'apm uninstall acme/evil-pkg'")

    monkeypatch.chdir(root)
    result = CliRunner().invoke(cli, ["uninstall", "acme/evil-pkg"])
    clear_apm_yml_cache()

    assert result.exit_code == 0, result.output
    assert yaml.safe_load((root / "apm.yml").read_text(encoding="utf-8"))["dependencies"] == {
        "apm": ["contoso/keep"]
    }
    assert _allowlist(root).passed


@pytest.mark.parametrize(
    ("deps", "locked", "children"),
    [
        pytest.param(
            ["acme/evil-pkg", "contoso/keep"],
            [_locked("acme/evil-pkg"), _locked("contoso/keep")],
            {"contoso/keep": ["acme/evil-pkg"]},
            id="a-surviving-package-rescues-the-denied-root",
        ),
        pytest.param(
            ["contoso/parent", "contoso/keep"],
            [
                _locked("contoso/parent"),
                _locked("contoso/keep"),
                _locked("acme/hidden", parent="contoso/parent"),
            ],
            {"contoso/parent": ["acme/hidden"], "contoso/keep": ["contoso/parent"]},
            id="a-surviving-package-rescues-the-allowed-parent-of-a-denied-descendant",
        ),
    ],
)
def test_a_rescued_removal_root_falls_back_to_the_admin(
    tmp_path: Path, capsys, deps, locked, children
) -> None:
    check = _allowlist(_project(tmp_path, deps, locked, children))

    assert _stderr(capsys, check)[2] == (
        "    Ask your org admin to allow it (policy source: org:contoso/.github)."
    )
    assert check.remediations == ()


@pytest.mark.parametrize(
    ("dev_section", "advised"),
    [
        pytest.param({"apm": ["acme/evil-pkg"]}, False, id="uninstall-empties-required-dev"),
        pytest.param(
            {"apm": ["acme/evil-pkg"], "mcp": ["contoso/server"]},
            True,
            id="another-dev-key-keeps-it-non-empty",
        ),
    ],
)
def test_emptying_a_required_dev_dependencies_field_falls_back_to_the_admin(
    tmp_path: Path, capsys, dev_section, advised
) -> None:
    root = _project(
        tmp_path,
        ["contoso/keep"],
        [_locked("acme/evil-pkg"), _locked("contoso/keep")],
        devDependencies=dev_section,
    )

    check = _allowlist(root, required_fields=("devDependencies",))

    assert _stderr(capsys, check)[2].startswith("    Run 'apm uninstall acme/evil-pkg'") is advised
    assert bool(check.remediations) is advised
