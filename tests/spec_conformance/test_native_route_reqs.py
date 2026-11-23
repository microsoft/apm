"""Opting a dependency out of native plugin activation (req-mf-030)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.models.apm_package import APMPackage, DependencyReference
from apm_cli.native_plugins.route import effective_native_route
from tests.spec_conformance._helpers import assert_spec_contains

pytestmark = pytest.mark.component


def _manifest(tmp_path: Path, body: str) -> APMPackage:
    path = tmp_path / "apm.yml"
    path.write_text("name: root\nversion: 1.0.0\n" + body, encoding="utf-8")
    return APMPackage.from_apm_yml(path)


@pytest.mark.req("req-mf-030")
def test_spec_states_the_native_opt_out() -> None:
    assert_spec_contains(
        "MUST NOT natively activate, on any target, a dependency that the root",
        "A `native` key binds only the declaration that carries",
    )


@pytest.mark.req("req-mf-030")
@pytest.mark.parametrize(
    ("top", "own", "expected"),
    [
        ("", None, True),
        ("", "false", False),
        ("", "true", True),
        ("native_plugins: false\n", None, False),
        ("native_plugins: false\n", "true", True),
        ("native_plugins: true\n", "false", False),
    ],
    ids=["auto", "own-false", "own-true", "default-off", "override-on", "override-off"],
)
def test_the_root_declaration_and_default_decide_the_route(
    tmp_path: Path, top: str, own: str | None, expected: bool
) -> None:
    entry = "    - path: ./pkg\n" + (f"      native: {own}\n" if own else "")
    manifest = _manifest(tmp_path, top + "dependencies:\n  apm:\n" + entry)
    dep = DependencyReference.parse("./pkg")

    assert effective_native_route(dep, manifest) is expected


@pytest.mark.req("req-mf-030")
@pytest.mark.parametrize("top", ["", "native_plugins: false\n"], ids=["auto", "off"])
def test_a_transitive_dependency_follows_the_root_default(tmp_path: Path, top: str) -> None:
    manifest = _manifest(
        tmp_path, top + "dependencies:\n  apm:\n    - path: ./parent\n      native: false\n"
    )
    child = DependencyReference.parse("./child")

    assert effective_native_route(child, manifest) is (top == "")


@pytest.mark.req("req-mf-030")
def test_non_boolean_values_are_rejected_naming_the_field(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="'native' field must be true or false"):
        DependencyReference.parse_from_dict({"path": "./pkg", "native": "no"})
    with pytest.raises(ValueError, match="'native_plugins' must be true or false"):
        _manifest(tmp_path, "native_plugins: maybe\n")


@pytest.mark.req("req-mf-030")
def test_an_opted_out_plugin_is_never_planned_natively(tmp_path: Path) -> None:
    from apm_cli.claude_plugins.admission import plan_native_claude_plugin

    manifest = _manifest(
        tmp_path, "dependencies:\n  apm:\n    - path: ./pkg\n      native: false\n"
    )
    info = SimpleNamespace(dependency_ref=DependencyReference.parse("./pkg"))

    def never() -> None:
        raise AssertionError("trust was consulted for an opted-out dependency")

    plan = plan_native_claude_plugin(
        info,
        package_name="./pkg",
        targets=[SimpleNamespace(name="claude")],
        scope=None,
        skill_subset=None,
        admit=never,
        skip_bin=False,
        manifest=manifest,
    )

    assert plan is None
