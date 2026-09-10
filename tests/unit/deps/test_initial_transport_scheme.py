"""Pure initial-scheme and shared custom-port warning contracts."""

from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from apm_cli.deps.transport_selection import (
    NoOpInsteadOfResolver,
    ProtocolPreference,
    TransportAttempt,
    TransportPlan,
    TransportSelector,
    fallback_port_warning,
    initial_transport_scheme,
)
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("spec", "explicit_scheme"),
    [
        ("git@gitlab.com:group/subgroup/repo.git", "ssh"),
        ("ssh://git@gitlab.com/group/repo.git", "ssh"),
        ("ssh://deploy@gitlab.example:2222/group/subgroup/repo.git", "ssh"),
        ("https://gitlab.example:8443/group/repo.git", "https"),
        ("http://gitlab.example:8080/group/repo.git", "http"),
        ("gitlab.com/group/repo", None),
        ("owner/repo", None),
        ("https://github.com/owner/repo", "https"),
        ("https://dev.azure.com/org/project/_git/repo", "https"),
    ],
)
@pytest.mark.parametrize("preference", list(ProtocolPreference))
@pytest.mark.parametrize("has_token", [False, True])
def test_initial_scheme_matches_selector(
    spec: str,
    explicit_scheme: str | None,
    preference: ProtocolPreference,
    has_token: bool,
) -> None:
    dep_ref = DependencyReference.parse(spec)
    expected = explicit_scheme or ("ssh" if preference is ProtocolPreference.SSH else "https")

    assert initial_transport_scheme(dep_ref, preference) == expected
    plan = TransportSelector(NoOpInsteadOfResolver()).select(
        dep_ref,
        cli_pref=preference,
        has_token=has_token,
    )
    assert plan.strict is True
    assert len(plan.attempts) == 1
    assert plan.attempts[0].scheme == expected
    assert plan.attempts[0].use_token is (has_token and expected == "https")


@pytest.mark.parametrize(
    ("preference", "expected"),
    [
        (ProtocolPreference.NONE, "https"),
        (ProtocolPreference.HTTPS, "https"),
        (ProtocolPreference.SSH, "ssh"),
    ],
)
def test_missing_reference_uses_preference(preference: ProtocolPreference, expected: str) -> None:
    assert initial_transport_scheme(None, preference) == expected


def test_explicit_scheme_is_case_normalized() -> None:
    dep_ref = SimpleNamespace(explicit_scheme="HTTPS")
    assert initial_transport_scheme(dep_ref, ProtocolPreference.SSH) == "https"


@pytest.mark.parametrize("schemes", [("ssh", "https"), ("https", "ssh")])
def test_custom_port_warning_preserves_text_and_docs_link(schemes: tuple[str, str]) -> None:
    dep_ref = DependencyReference.parse("ssh://deploy@gitlab.example:2222/group/repo.git")
    plan = TransportPlan(
        attempts=[TransportAttempt(scheme, False, scheme) for scheme in schemes],
        strict=False,
    )

    warning = fallback_port_warning(dep_ref, plan)

    assert warning is not None
    body, docs_url = warning.rsplit("See: ", 1)
    assert body == (
        f"Custom port 2222 on {dep_ref.host}/{dep_ref.repo_url}: "
        f"if {schemes[0].upper()} fails, APM will retry over "
        f"{schemes[1].upper()} on the same port.\n"
        "    Pin the URL scheme, or drop "
        "--allow-protocol-fallback to fail fast.\n"
        "    "
    )
    parsed = urlsplit(docs_url)
    assert (parsed.scheme, parsed.hostname, parsed.path, parsed.fragment) == (
        "https",
        "microsoft.github.io",
        "/apm/guides/dependencies/",
        "restoring-the-legacy-permissive-chain",
    )


@pytest.mark.parametrize(
    ("spec", "strict", "schemes"),
    [
        (None, False, ("ssh", "https")),
        ("ssh://git@gitlab.example/group/repo", False, ("ssh", "https")),
        ("ssh://git@gitlab.example:2222/group/repo", True, ("ssh", "https")),
        ("ssh://git@gitlab.example:2222/group/repo", False, ("ssh",)),
        ("ssh://git@gitlab.example:2222/group/repo", False, ("https", "https")),
        ("ssh://git@gitlab.example:2222/group/repo", False, ("http", "ssh")),
        ("ssh://git@gitlab.example:2222/group/repo", False, ()),
    ],
)
def test_no_warning_without_custom_port_cross_protocol_plan(
    spec: str | None, strict: bool, schemes: tuple[str, ...]
) -> None:
    dep_ref = DependencyReference.parse(spec) if spec else None
    plan = TransportPlan(
        attempts=[TransportAttempt(scheme, False, scheme) for scheme in schemes],
        strict=strict,
    )
    assert fallback_port_warning(dep_ref, plan) is None
