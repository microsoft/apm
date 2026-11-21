"""Deprecation owners keep their trigger, repetition and scope (#4019).

The catalog in ``core/deprecations.py`` owns only the words. These tests pin
the behavior each domain owner keeps: how often a warning repeats, which scope
it is collected into, and the exact Python ``DeprecationWarning`` text. Python
warnings are hidden by default, so every test here enables the filter
explicitly instead of relying on the interpreter's default.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from apm_cli.core import apm_yml, target_detection
from apm_cli.deps.registry_proxy import RegistryConfig
from apm_cli.marketplace.migration import load_marketplace_config
from apm_cli.marketplace.yml_schema import _parse_outputs
from apm_cli.policy.parser import validate_policy
from apm_cli.registry.client import SimpleRegistryClient

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-user-facing-output-diagnostics")]


def _deprecations(caught: list[warnings.WarningMessage]) -> list[tuple[type, str]]:
    return [
        (item.category, str(item.message))
        for item in caught
        if issubclass(item.category, DeprecationWarning)
    ]


def test_artifactory_aliases_raise_one_python_warning_each(monkeypatch) -> None:
    for name in ("PROXY_REGISTRY_URL", "PROXY_REGISTRY_TOKEN", "PROXY_REGISTRY_ONLY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARTIFACTORY_BASE_URL", "https://art.example.invalid/api")
    monkeypatch.setenv("ARTIFACTORY_APM_TOKEN", "token")
    monkeypatch.setenv("ARTIFACTORY_ONLY", "1")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config = RegistryConfig.from_env()

    assert config is not None and config.enforce_only
    assert _deprecations(caught) == [
        (DeprecationWarning, "ARTIFACTORY_BASE_URL is deprecated; use PROXY_REGISTRY_URL instead."),
        (
            DeprecationWarning,
            "ARTIFACTORY_APM_TOKEN is deprecated; use PROXY_REGISTRY_TOKEN instead.",
        ),
        (DeprecationWarning, "ARTIFACTORY_ONLY is deprecated; use PROXY_REGISTRY_ONLY instead."),
    ]


def test_registry_get_server_info_alias_warns_and_forwards(monkeypatch) -> None:
    client = SimpleRegistryClient("https://registry.example.invalid")
    monkeypatch.setattr(client, "get_server", lambda name: {"name": name})

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert client.get_server_info("srv") == {"name": "srv"}

    assert _deprecations(caught) == [
        (
            DeprecationWarning,
            "SimpleRegistryClient.get_server_info(server_name) is deprecated; "
            "use SimpleRegistryClient.get_server(server_name, version='latest'). "
            "The parameter now means a server name per MCP Registry v0.1.",
        )
    ]


def test_agents_target_raises_its_own_python_warning_category() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        target_detection.parse_target_field("agents")

    assert _deprecations(caught) == [
        (
            target_detection.AgentsTargetDeprecationWarning,
            "'--target agents' is deprecated -- it maps to 'copilot' (.github/), not "
            "'.agents/'. Use '--target copilot' or '--target agent-skills' "
            "(.agents/skills/). Removal in v1.0.",
        )
    ]
    assert target_detection.agents_alias_was_detected()


def test_legacy_all_target_warns_once_per_process(monkeypatch) -> None:
    shown: list[str] = []
    monkeypatch.setattr(
        "apm_cli.utils.console._rich_warning", lambda message, **_: shown.append(message)
    )
    apm_yml._reset_legacy_all_warning()
    try:
        apm_yml.parse_targets_field({"targets": ["all"]})
        apm_yml.parse_targets_field({"targets": ["all", "claude"]})
    finally:
        apm_yml._reset_legacy_all_warning()

    assert shown == [
        "'all' in apm.yml targets is deprecated and will become a hard error in a "
        "future release. APM is treating the field as omitted so the current command's "
        "default target selection applies. If you maintain this package, remove 'all' "
        "from the manifest."
    ]


def test_target_alias_flag_warns_once_per_alias_across_flags(monkeypatch) -> None:
    shown: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "apm_cli.utils.console._rich_warning",
        lambda message, **kwargs: shown.append((message, kwargs)),
    )
    warned = target_detection._deprecated_flag_aliases_warned
    before = set(warned)
    warned.clear()
    try:
        target_detection.warn_deprecated_target_alias_flag("claude,vscode")
        target_detection.warn_deprecated_target_alias_flag("vscode", flag="--runtime")
    finally:
        warned.clear()
        warned.update(before)

    assert shown == [
        (
            "--target: replace `vscode` with `copilot`; `vscode` is deprecated and "
            "will be removed in a future release.",
            {"symbol": "warning", "err": True},
        )
    ]


def test_bin_deploy_warning_is_returned_once_per_validation() -> None:
    data = {"name": "p", "version": "1.0.0", "bin_deploy": {"deny_all": True}}
    expected = (
        "'bin_deploy' is deprecated; use 'executables' (deny_all/deny) instead. "
        "bin_deploy is still honored as a bin-scoped deny alias for one minor cycle."
    )

    for _ in range(2):
        errors, policy_warnings = validate_policy(data)
        assert errors == []
        assert policy_warnings.count(expected) == 1


def test_legacy_marketplace_yml_warns_on_every_load(tmp_path: Path) -> None:
    (tmp_path / "marketplace.yml").write_text(
        "name: m\ndescription: d\nversion: 1.0.0\nowner:\n  name: o\npackages: []\n",
        encoding="utf-8",
    )
    shown: list[str] = []

    for _ in range(2):
        load_marketplace_config(tmp_path, warn_callback=shown.append)

    assert (
        shown
        == [
            "marketplace.yml is deprecated. Run 'apm marketplace migrate' to fold "
            "marketplace.yml into apm.yml."
        ]
        * 2
    )


@pytest.mark.parametrize(
    ("raw", "names"),
    [(["claude", "codex"], "claude, codex"), ("claude", "claude")],
    ids=["list", "string"],
)
def test_marketplace_outputs_list_form_collects_its_warning(raw, names: str) -> None:
    sink: list[str] = []

    outputs, _specs = _parse_outputs(raw, warnings_sink=sink)

    map_lines = "\n".join(f"        {name}: {{}}" for name in outputs)
    assert sink == [
        f"outputs: [{names}] is deprecated; use the map form:\n\n"
        f"      outputs:\n{map_lines}\n\n"
        "    The list form will be removed in v0.15."
    ]
