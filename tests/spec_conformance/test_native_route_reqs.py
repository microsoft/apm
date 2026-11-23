"""Opting a dependency out of native plugin activation (req-mf-030)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.models.apm_package import APMPackage, DependencyReference
from apm_cli.native_plugins.route import effective_native_route
from tests.spec_conformance._helpers import assert_spec_contains

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-claude-native-plugin-registration")]


def _manifest(tmp_path: Path, body: str) -> APMPackage:
    tmp_path.mkdir(parents=True, exist_ok=True)
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


@pytest.mark.req("req-mf-030")
def test_spec_states_the_folded_route_clauses() -> None:
    assert_spec_contains(
        "except where [req-tg-011](#req-tg-011) makes the dependency opaque",
        "under [req-tg-013](#req-tg-013) MUST NOT register such a dependency there either",
        "or a restore that does not rewrite the lockfile, when the lockfile records a",
        "per [req-na-004](#req-na-004) from the previous and the next lockfile",
    )


@pytest.mark.req("req-mf-030")
def test_frozen_names_a_locked_native_plugin_the_manifest_opts_out(tmp_path: Path) -> None:
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.native_plugins.record import NativePluginRecord
    from apm_cli.native_plugins.route import frozen_route_change_reasons

    lock = LockFile()
    for key, native in (("acme/native", True), ("acme/loose", False)):
        locked = LockedDependency(repo_url=key, resolved_commit="a" * 40)
        if native:
            locked.native_plugin = NativePluginRecord(
                "n", "sha256:" + "0" * 64, "apm-p-00000000", "./.agents/plugins"
            )
        lock.dependencies[key] = locked
    entries = "".join(
        f"    - git: {key}\n      native: false\n" for key in ("acme/native", "acme/loose")
    )
    opted_out = _manifest(tmp_path, "dependencies:\n  apm:\n" + entries)

    reasons = frozen_route_change_reasons(lock, opted_out)

    assert len(reasons) == 1 and "acme/native" in reasons[0]
    auto = _manifest(
        tmp_path / "auto", "dependencies:\n  apm:\n    - acme/native\n    - acme/loose\n"
    )
    assert frozen_route_change_reasons(lock, auto) == []


@pytest.mark.req("req-mf-030")
@pytest.mark.parametrize("order", [("true", "false"), ("false", "true")], ids=["f-last", "f-first"])
def test_conflicting_root_declarations_resolve_false_in_any_order(
    tmp_path: Path, order: tuple[str, str]
) -> None:
    entries = "".join(f"    - path: ./pkg\n      native: {value}\n" for value in order)
    manifest = _manifest(tmp_path, "dependencies:\n  apm:\n" + entries)

    assert effective_native_route(DependencyReference.parse("./pkg"), manifest) is False


@pytest.mark.req("req-mf-030")
@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("native_plugins: no\n", "'native_plugins' must be true or false, got 'no'"),
        (
            "dependencies:\n  apm:\n    - path: ./pkg\n      native: Off\n",
            "'native' field must be true or false, got 'Off'",
        ),
        (
            "devDependencies:\n  apm:\n    - path: ./pkg\n      native: True\n",
            "'native' field must be true or false, got 'True'",
        ),
    ],
    ids=["native_plugins-no", "native-Off", "dev-native-True"],
)
def test_yaml_1_1_boolean_spellings_are_rejected_on_load(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _manifest(tmp_path, body)


@pytest.mark.req("req-mf-030")
def test_an_explicit_bool_tag_is_accepted_on_load(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path, "dependencies:\n  apm:\n    - path: ./pkg\n      native: !!bool false\n"
    )

    assert effective_native_route(DependencyReference.parse("./pkg"), manifest) is False


@pytest.mark.req("req-mf-030")
@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "dependencies:\n  apm:\n    - <<: {native: no}\n      path: ./pkg\n",
            "'native' field must be true or false, got 'no'",
        ),
        (
            "<<: {native_plugins: off}\n",
            "'native_plugins' must be true or false, got 'off'",
        ),
        (
            "dependencies:\n  apm:\n    - <<: [{alias: k}, {native: Yes}]\n      path: ./pkg\n",
            "'native' field must be true or false, got 'Yes'",
        ),
    ],
    ids=["dependency-merge", "top-level-merge", "sequence-merge"],
)
def test_a_misspelled_boolean_behind_a_merge_key_is_rejected(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _manifest(tmp_path, body)
