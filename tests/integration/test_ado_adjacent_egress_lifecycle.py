"""Adjacent Azure DevOps egress keeps working when auth routing changes (#3752).

The credential classifier and redirect owner land under every command that
reaches Azure DevOps, not only ``install``. These scenarios pin the
neighbouring commands, with only a Git credential helper configured:

* ``marketplace add`` / ``browse`` / ``update`` over the ADO Items API;
* ``view <ado-package> versions`` (remote ref enumeration);
* local-only commands (``deps list`` / ``tree`` / ``why``) never egress.

No request carrying a credential may reach the sign-in host, and no
credential is written to APM state.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.integration._ado_auth_kit import (
    HELPER_SECRET,
    HELPER_USER,
    INSTALL,
    ORG,
    PROJECT,
    SOURCE,
    AdoAuthScenario,
    ado_dependency_entry,
    describe,
    manifest,
)
from tests.utils.local_ado_org_fixture import ADO_HOST, SIGNIN_HOST
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_scripted_https_proxy import basic_authorization

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX credential helper launcher"),
]

_PAT = "ado-adjacent-pat-sentinel-77e4"
_MARKETPLACE = {
    "name": "ado-tools",
    "owner": {"name": "contoso"},
    "plugins": [{"name": "tool-x", "source": "./tools/x", "version": "1.0.0"}],
}


def _publish_marketplace(scenario: AdoAuthScenario) -> str:
    root = scenario.isolated.package_root / "tools"
    (root / "tools" / "x").mkdir(parents=True)
    (root / "marketplace.json").write_text(json.dumps(_MARKETPLACE, indent=2), encoding="utf-8")
    (root / "tools" / "x" / "apm.yml").write_text(
        "name: tool-x\nversion: 1.0.0\n", encoding="utf-8"
    )
    repository = scenario.repositories.create("tools", source_tree=root)
    scenario.repositories.commit(repository, message="marketplace")
    scenario.ado.add_repository(ORG, PROJECT, "tools", repository.origin)
    return f"https://{ADO_HOST}/{ORG}/{PROJECT}/_git/tools"


def _assert_signin_untouched(proxy) -> None:
    """The sign-in redirect is classified, never followed (with or without a credential)."""
    followed = [o for o in proxy.observations if o.host == SIGNIN_HOST and o.method != "CONNECT"]
    assert followed == [], [o.target for o in followed]
    presented = {o.header("Authorization") for o in proxy.requests_to(ADO_HOST)} - {None}
    assert presented <= {basic_authorization(HELPER_USER, HELPER_SECRET)}, presented


def _assert_state_secret_free(scenario: AdoAuthScenario) -> None:
    for path in scenario.isolated.config_root.rglob("*"):
        if path.is_file():
            assert HELPER_SECRET not in path.read_text(encoding="utf-8", errors="replace"), path


def test_ado_marketplace_add_browse_update_through_credential_helper(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper()
    url = _publish_marketplace(scenario)
    cwd = scenario.project(manifest=manifest())
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        add = scenario.run(
            ("marketplace", "add", url, "--name", "ado-tools"), cwd=cwd, env=env, step="mkt-add"
        )
        _assert_signin_untouched(proxy)
        assert add.returncode == 0, describe(add, proxy)
        browse = scenario.run(
            ("marketplace", "browse", "ado-tools"), cwd=cwd, env=env, step="browse"
        )
        assert browse.returncode == 0, describe(browse, proxy)
        assert "tool-x" in browse.stdout + browse.stderr, describe(browse, proxy)
        update = scenario.run(
            ("marketplace", "update", "ado-tools"), cwd=cwd, env=env, step="mkt-update"
        )
        assert update.returncode == 0, describe(update, proxy)
        _assert_signin_untouched(proxy)
    _assert_state_secret_free(scenario)


def test_ado_view_versions_through_credential_helper(tmp_path: Path, apm_binary_path: Path) -> None:
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper()
    cwd = scenario.project(manifest=manifest())
    with scenario.serve() as proxy:
        result = scenario.run(
            ("view", SOURCE, "versions"), cwd=cwd, env=scenario.env(proxy), step="view-versions"
        )
        assert result.returncode == 0, describe(result, proxy)
        assert "main" in result.stdout, describe(result, proxy)
        _assert_signin_untouched(proxy)


def test_local_dependency_queries_do_not_egress(tmp_path: Path, apm_binary_path: Path) -> None:
    """Regression lock: deps list/tree/why read the lock only; no credential is needed."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.ado.accepted_authorizations.add(basic_authorization("", _PAT))
    project = scenario.project(manifest=manifest(ado_dependency_entry()))
    with scenario.serve() as proxy:
        seeded = scenario.run(
            INSTALL, cwd=project, env=scenario.env(proxy, {"ADO_APM_PAT": _PAT}), step="seed"
        )
        assert seeded.returncode == 0, describe(seeded, proxy)
        proxy.clear()
        env = scenario.env(proxy)
        for args in (("deps", "list"), ("deps", "tree"), ("deps", "why", "ado-helper")):
            result = scenario.run(args, cwd=project, env=env, step="-".join(args))
            assert result.returncode in (0, 1), describe(result, proxy)
            assert _PAT not in result.stdout + result.stderr
        assert proxy.requests_to(ADO_HOST) == (), [o.target for o in proxy.requests_to(ADO_HOST)]
