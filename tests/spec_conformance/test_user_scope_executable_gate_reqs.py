"""req-sc-009 at user scope: the user manifest's approval block gates deployment.

At user scope the deploy root is ``$HOME`` while the manifest lives in the
user location (``$APM_HOME/apm.yml``). The approval block that req-sc-009
makes fail closed is the one in that manifest.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.install.template import effective_allow_map
from apm_cli.security.executables import materialize_exec_map
from apm_cli.security.trust import McpAdmission


def _user_install_ctx(home):
    return SimpleNamespace(
        scope=InstallScope.USER,
        project_root=home,
        policy_fetch=None,
        apm_package=SimpleNamespace(allow_executables=None),
        logger=None,
        dry_run=True,
    )


def _mcp_admission_map(home):
    admission = McpAdmission.for_project(home, user_scope=True, no_policy=True)
    return materialize_exec_map(admission.context)


@pytest.mark.req("req-sc-009")
@pytest.mark.parametrize(
    ("exec_type", "resolve"),
    [
        ("hooks", lambda home: effective_allow_map(_user_install_ctx(home))),
        ("mcp", _mcp_admission_map),
    ],
    ids=["install-hooks", "mcp-admission"],
)
@pytest.mark.parametrize(
    ("listed", "approved"),
    [(False, False), (True, True)],
    ids=["unlisted-denied", "listed-approved"],
)
def test_user_scope_install_fails_closed_on_user_manifest_block(
    tmp_path, monkeypatch, exec_type, resolve, listed, approved
) -> None:
    home, apm_home = tmp_path / "home", tmp_path / "apm-home"
    home.mkdir()
    apm_home.mkdir()
    # A deploy-root manifest is not the user manifest: its grants are ignored.
    (home / "apm.yml").write_text(
        f"name: stray\nversion: 1.0.0\nexecutables:\n  allow:\n    owner/decoy:\n"
        f"      {exec_type}: true\n",
        encoding="utf-8",
    )
    allow_block = f"\n    owner/pkg:\n      {exec_type}: true" if listed else " {}"
    (apm_home / "apm.yml").write_text(
        f"name: user\nversion: 1.0.0\nexecutables:\n  allow:{allow_block}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APM_HOME", str(apm_home))

    allow_map = resolve(home)

    assert allow_map is not None, "user manifest approval block must enable the gate"
    assert bool(allow_map.get("owner/pkg", {}).get(exec_type)) is approved
    assert not allow_map.get("owner/decoy", {}).get(exec_type)
