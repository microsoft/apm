"""Azure DevOps installs authenticate through the Git credential helper (#3752).

Users with Git Credential Manager (or any ``git credential`` helper) and no
``ADO_APM_PAT`` / Azure CLI must be able to install, re-install from a lock,
update and check for updates on Azure DevOps dependencies, exactly as
``git clone`` would. The helper is asked with the repository path
(``org/project/_git/repo``) and its username is presented with the password.
A configured PAT keeps precedence and the helper is not consulted.

Each scenario drives the installed CLI against an impersonated
``dev.azure.com`` (scripted HTTPS proxy, real ``git http-backend``) that only
accepts the configured credential and answers anonymous requests the way
Azure DevOps does (401 for Git, 302 to the sign-in host for REST).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration._ado_auth_kit import (
    HELPER_PATH,
    HELPER_SECRET,
    HELPER_USER,
    INSTALL,
    SKILL_PATH,
    SOURCE,
    AdoAuthScenario,
    ado_dependency_entry,
    describe,
    flat,
    manifest,
    received_helper_credential,
    skill_document,
)
from tests.utils.local_ado_org_fixture import ADO_HOST
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_scripted_https_proxy import basic_authorization

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX credential helper launcher"),
]

_PAT = "ado-pat-precedence-sentinel-91c2"


def _locked(project: Path) -> dict[str, object]:
    return load_yaml(project / "apm.lock.yaml")["dependencies"][0]


def _assert_helper_scoped(scenario: AdoAuthScenario) -> None:
    gets = scenario.helper.gets() if scenario.helper else ()
    assert gets, "credential helper was never asked"
    for event in gets:
        fields = event["fields"]
        assert fields.get("host") == ADO_HOST, event
        assert fields.get("protocol") == "https", event
        assert fields.get("path", "").removesuffix(".git") == HELPER_PATH, event


def _assert_no_secret_persisted(project: Path) -> None:
    for relative in ("apm.yml", "apm.lock.yaml"):
        path = project / relative
        if path.exists():
            assert HELPER_SECRET not in path.read_text(encoding="utf-8")
            assert HELPER_USER not in path.read_text(encoding="utf-8")


def test_ado_pat_keeps_precedence_over_credential_helper(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Regression lock: ADO_APM_PAT is presented first; the helper is never asked."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    helper = scenario.install_helper()
    scenario.ado.accepted_authorizations.add(basic_authorization("", _PAT))
    project = scenario.project(manifest=manifest(ado_dependency_entry()))
    with scenario.serve() as proxy:
        env = scenario.env(proxy, {"ADO_APM_PAT": _PAT})
        result = scenario.run(INSTALL, cwd=project, env=env, step="pat-install")
        assert result.returncode == 0, describe(result, proxy)
        assert helper.events() == ()
        assert not received_helper_credential(proxy)
    assert (project / SKILL_PATH).read_text(encoding="utf-8") == skill_document("version one")
    assert _PAT not in (project / "apm.lock.yaml").read_text(encoding="utf-8")

    cli_project = scenario.project("cli-consumer", manifest=manifest())
    with scenario.serve() as proxy:
        env = scenario.env(proxy, {"ADO_APM_PAT": _PAT})
        result = scenario.run(
            (*INSTALL, f"{SOURCE}/skills/ado-helper"), cwd=cli_project, env=env, step="pat-cli"
        )
        assert result.returncode == 0, describe(result, proxy)
        assert helper.gets() == ()


def test_ado_cli_install_uses_credential_helper_without_pat_or_az(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """``apm install <ado-url>`` succeeds when only a credential helper knows the user."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper()
    project = scenario.project(manifest=manifest())
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        result = scenario.run(
            (*INSTALL, f"{SOURCE}/skills/ado-helper"), cwd=project, env=env, step="cli-install"
        )
        assert result.returncode == 0, describe(result)
        assert received_helper_credential(proxy)
        presented = {
            o.header("Authorization")
            for o in proxy.requests_to(ADO_HOST)
            if o.header("Authorization")
        }
        assert presented <= {basic_authorization(HELPER_USER, HELPER_SECRET)}, presented
    _assert_helper_scoped(scenario)
    assert (project / SKILL_PATH).read_text(encoding="utf-8") == skill_document("version one")
    _assert_no_secret_persisted(project)


def test_ado_manifest_install_frozen_update_outdated_through_credential_helper(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The whole lock lifecycle works with only a helper credential, lock stays secret-free."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper()
    project = scenario.project(manifest=manifest(ado_dependency_entry()))
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        install = scenario.run(INSTALL, cwd=project, env=env, step="install")
        assert install.returncode == 0, describe(install)
        assert _locked(project)["resolved_commit"] == scenario.first_commit.sha
        lock_bytes = (project / "apm.lock.yaml").read_bytes()

        frozen = scenario.run((*INSTALL, "--frozen"), cwd=project, env=env, step="frozen")
        assert frozen.returncode == 0, describe(frozen)
        assert (project / "apm.lock.yaml").read_bytes() == lock_bytes

        second = scenario.advance("version two")
        outdated = scenario.run(("outdated",), cwd=project, env=env, step="outdated")
        assert outdated.returncode == 0, describe(outdated)
        assert "unknown" not in flat(outdated).lower() or second[:7] in flat(outdated)

        update = scenario.run(
            ("update", "--yes", "--target", "copilot", "--parallel-downloads", "0"),
            cwd=project,
            env=env,
            step="update",
        )
        assert update.returncode == 0, describe(update)
        assert _locked(project)["resolved_commit"] == second
    assert (project / SKILL_PATH).read_text(encoding="utf-8") == skill_document("version two")
    _assert_helper_scoped(scenario)
    _assert_no_secret_persisted(project)


@pytest.mark.parametrize("scope", ["global", "apm-home"])
def test_ado_user_scope_install_through_credential_helper(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    """``-g`` (and a relocated APM_HOME) use the same helper chain as project scope."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper()
    cwd = scenario.project(manifest=manifest())
    overrides: dict[str, str] = {}
    if scope == "apm-home":
        relocated = scenario.isolated.root / "relocated-apm-home"
        relocated.mkdir()
        overrides["APM_HOME"] = str(relocated)
    with scenario.serve() as proxy:
        env = scenario.env(proxy, overrides)
        result = scenario.run(
            ("install", "-g", "--target", "copilot", f"{SOURCE}/skills/ado-helper"),
            cwd=cwd,
            env=env,
            step=f"user-install-{scope}",
        )
        assert result.returncode == 0, describe(result)
        assert received_helper_credential(proxy)
    _assert_helper_scoped(scenario)
    home = Path(env["APM_HOME"])
    for path in home.rglob("*"):
        if path.is_file() and path.suffix in (".yml", ".yaml", ".json"):
            assert HELPER_SECRET not in path.read_text(encoding="utf-8", errors="replace")


def test_ado_helper_credential_outcome_is_reported_back_to_git(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Git's helper hears approve after success and reject after a 401, once each (#3765)."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    helper = scenario.install_helper()
    project = scenario.project(manifest=manifest(ado_dependency_entry()))
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        result = scenario.run(INSTALL, cwd=project, env=env, step="accepted")
        assert result.returncode == 0, describe(result, proxy)
    stores = [event for event in helper.events() if event["action"] == "store"]
    assert len(stores) == 1, helper.events()
    assert [event for event in helper.events() if event["action"] == "erase"] == []
    stored = stores[0]["fields"]
    assert (stored["host"], stored["path"].removesuffix(".git")) == (ADO_HOST, HELPER_PATH)
    assert (stored["username"], stored["password"]) == (HELPER_USER, HELPER_SECRET)
    assert stores[0]["env"]["GCM_INTERACTIVE"] in ("0", "never", "false"), stores[0]

    # The server now refuses the stored credential (expired or revoked).
    scenario.ado.accepted_authorizations.clear()
    before = len(helper.events())
    stale = scenario.project("stale-consumer", manifest=manifest(ado_dependency_entry()))
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        result = scenario.run(INSTALL, cwd=stale, env=env, step="rejected")
        assert result.returncode != 0, describe(result, proxy)
    later = helper.events()[before:]
    erases = [event for event in later if event["action"] == "erase"]
    assert len(erases) == 1, later
    assert [event for event in later if event["action"] == "store"] == []
    erased = erases[0]["fields"]
    assert (erased["host"], erased["path"].removesuffix(".git")) == (ADO_HOST, HELPER_PATH)
    assert erased["password"] == HELPER_SECRET
    assert HELPER_SECRET not in flat(result)


def test_ado_noninteractive_credential_fill_disables_helper_ui_in_ci(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """With CI=1 and no TTY the helper is asked with interaction disabled."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    helper = scenario.install_helper()
    project = scenario.project(manifest=manifest(ado_dependency_entry()))
    with scenario.serve() as proxy:
        env = scenario.env(proxy, {"CI": "1"})
        result = scenario.run(INSTALL, cwd=project, env=env, step="ci-install")
        assert result.returncode == 0, describe(result)
    gets = helper.gets()
    assert gets
    for event in gets:
        assert event["env"]["GCM_INTERACTIVE"] in ("0", "never", "false"), event
        assert event["env"]["GIT_TERMINAL_PROMPT"] == "0", event


def test_ado_exhausted_chain_names_every_step_in_order(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """When the helper has nothing, the error lists PAT, Azure CLI and the helper in order."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    helper = scenario.install_helper(respond=False)
    project = scenario.project(manifest=manifest(ado_dependency_entry()))
    before = (project / "apm.yml").read_bytes()
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        result = scenario.run(INSTALL, cwd=project, env=env, step="exhausted")
    assert result.returncode != 0, describe(result)
    text = flat(result)
    assert helper.gets(), "helper was not consulted before giving up"
    positions = [text.find(step) for step in ("ADO_APM_PAT", "az", "credential")]
    assert all(position >= 0 for position in positions), text
    assert positions == sorted(positions), text
    assert HELPER_SECRET not in text
    assert (project / "apm.yml").read_bytes() == before
    assert not (project / "apm.lock.yaml").exists()


def test_ado_resolved_credential_is_not_replaced_by_ambient_netrc(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """An unrelated ``~/.netrc`` entry for the host never replaces the resolved credential."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.ado.accepted_authorizations.add(basic_authorization("", _PAT))
    netrc = scenario.isolated.home / ".netrc"
    netrc.write_text(
        f"machine {ADO_HOST}\n  login netrc-user\n  password netrc-other-sentinel-55aa\n",
        encoding="ascii",
    )
    netrc.chmod(0o600)
    project = scenario.project(manifest=manifest(ado_dependency_entry()))
    with scenario.serve() as proxy:
        env = scenario.env(proxy, {"ADO_APM_PAT": _PAT})
        result = scenario.run(
            (*INSTALL, f"{SOURCE}/skills/ado-helper"), cwd=project, env=env, step="netrc"
        )
        rest = [
            o
            for o in proxy.requests_to(ADO_HOST)
            if "/_apis/" in o.path and o.header("Authorization")
        ]
        leaked = proxy.hosts_receiving("netrc-other-sentinel-55aa")
        evidence = describe(result, proxy)
    assert leaked == set(), [(o.method, o.path) for o in rest]
    assert result.returncode == 0, evidence


def test_ado_lock_records_no_auth_material(tmp_path: Path, apm_binary_path: Path) -> None:
    """Regression lock: a PAT-authenticated lock carries coordinates only."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.ado.accepted_authorizations.add(basic_authorization("", _PAT))
    project = scenario.project(manifest=manifest(ado_dependency_entry()))
    with scenario.serve() as proxy:
        env = scenario.env(proxy, {"ADO_APM_PAT": _PAT})
        result = scenario.run(INSTALL, cwd=project, env=env, step="lock")
    assert result.returncode == 0, describe(result)
    locked = _locked(project)
    assert locked["host"] == ADO_HOST
    serialized = json.dumps(locked, sort_keys=True, default=str)
    assert _PAT not in serialized and "Authorization" not in serialized
