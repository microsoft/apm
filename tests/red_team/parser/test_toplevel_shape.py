"""RED-TEAM: non-dict top-level apm.yml crashes lifecycle discovery.

GENUINE BREAK (D4-1, HIGH). ``parse_apm_yml_lifecycle`` does::

    return _entries_from_lifecycle_map((data or {}).get("lifecycle"), ...)

The ``.get("lifecycle")`` sits OUTSIDE the try/except that guards
``load_yaml``. When an ``apm.yml`` parses to a non-dict, non-empty value
-- a YAML list, a bare scalar, or a plain string -- ``data`` is truthy and
has no ``.get``, so an uncaught ``AttributeError`` escapes the parser. This
propagates through ``discover_scripts`` (which calls the parser unguarded
for both the user and project tiers) and therefore through every caller:
the ``apm lifecycle`` listing AND the install/update/uninstall lifecycle
firing path (``build_runner_from_context`` calls ``discover_scripts``
without a guard).

Attack: ship a repo whose ``apm.yml`` top level is a YAML list/scalar (a
trivial typo or a deliberately malformed manifest in an untrusted clone).
``apm install`` then crashes while wiring lifecycle scripts.

These tests assert the ROBUST expectation (parse/discover/list degrade to
an empty result, exactly as they already do for ``null`` and ``{}``). They
FAIL on head, demonstrating the break; they will pass once the ``.get`` is
moved inside the guard or behind an ``isinstance(data, dict)`` check.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from .conftest import write_apm_yml

NON_DICT_TOP_LEVELS = {
    "list": "- a\n- b\n- c\n",
    "string": '"just a string"\n',
    "int": "42\n",
    "float": "3.14\n",
}

# Top-levels that already degrade safely (control group -- must stay robust).
SAFE_TOP_LEVELS = {
    "null": "null\n",
    "empty": "",
    "dict_no_lifecycle": "name: demo\nversion: 1\n",
}


@pytest.mark.parametrize("name", sorted(SAFE_TOP_LEVELS))
def test_parser_robust_on_safe_top_levels(tmp_path, name):
    """Control: null / empty / lifecycle-less dicts already yield []."""
    from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle

    doc = write_apm_yml(tmp_path, SAFE_TOP_LEVELS[name])
    assert parse_apm_yml_lifecycle(doc, "project") == []


@pytest.mark.parametrize("name", sorted(NON_DICT_TOP_LEVELS))
def test_parser_must_not_crash_on_non_dict_top_level(tmp_path, name):
    """RED: a non-dict top-level must degrade to [], not raise."""
    from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle

    doc = write_apm_yml(tmp_path, NON_DICT_TOP_LEVELS[name])
    try:
        result = parse_apm_yml_lifecycle(doc, "project")
    except Exception as exc:
        pytest.fail(
            f"parse_apm_yml_lifecycle raised on {name} top-level apm.yml: "
            f"{type(exc).__name__}: {exc}"
        )
    assert result == []


@pytest.mark.parametrize("name", sorted(NON_DICT_TOP_LEVELS))
def test_discover_scripts_must_not_crash_on_non_dict_top_level(
    tmp_path, policy_dir, isolated_home, name
):
    """RED: discover_scripts must survive a malformed project apm.yml."""
    from apm_cli.core.lifecycle_scripts import discover_scripts

    write_apm_yml(tmp_path, NON_DICT_TOP_LEVELS[name])
    try:
        result = discover_scripts(project_root=str(tmp_path))
    except Exception as exc:
        pytest.fail(
            f"discover_scripts raised on {name} top-level apm.yml: {type(exc).__name__}: {exc}"
        )
    assert result == []


def test_lifecycle_list_cli_must_not_crash_on_list_apm_yml(
    tmp_path, monkeypatch, policy_dir, isolated_home
):
    """RED: `apm lifecycle` listing must degrade gracefully, not traceback."""
    from apm_cli.commands.lifecycle import lifecycle

    monkeypatch.chdir(tmp_path)
    write_apm_yml(tmp_path, "- a\n- b\n")

    result = CliRunner().invoke(lifecycle, [], catch_exceptions=True)
    assert result.exception is None, (
        f"apm lifecycle crashed on a list-typed apm.yml: {result.exception!r}"
    )
    assert result.exit_code == 0


def test_user_tier_non_dict_apm_yml_must_not_crash_discovery(tmp_path, policy_dir, monkeypatch):
    """RED: a malformed USER ~/.apm/apm.yml must not crash discovery either."""
    from apm_cli.core.lifecycle_scripts import discover_scripts

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("APM_HOME", str(home))
    (home / "apm.yml").write_text("- bad\n- user\n- manifest\n", encoding="utf-8")

    project = tmp_path / "proj"
    project.mkdir()

    try:
        result = discover_scripts(project_root=str(project))
    except Exception as exc:
        pytest.fail(
            f"discover_scripts raised on non-dict user apm.yml: {type(exc).__name__}: {exc}"
        )
    assert result == []
