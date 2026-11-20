"""Fragment refs in marketplace registration sources (#4200)."""

import pytest

from apm_cli.marketplace.registration import parse_registration_source

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-marketplace-source-admission")]


@pytest.mark.parametrize(
    ("source", "url", "ref"),
    [
        ("acme/mkt#v1.0.0", "https://github.com/acme/mkt", "v1.0.0"),
        ("gitlab.com/team/mkt#release/x", "https://gitlab.com/team/mkt", "release/x"),
        ("https://gitlab.com/team/mkt#v2", "https://gitlab.com/team/mkt", "v2"),
        ("acme/mkt", "https://github.com/acme/mkt", "main"),
    ],
)
def test_registration_source_honors_shorthand_fragment_ref(source, url, ref):
    """#4200: OWNER/REPO#ref and HOST/OWNER/REPO#ref select the ref like a full URL."""
    parsed = parse_registration_source(source)

    assert (parsed.url, parsed.ref) == (url, ref)


def test_registration_source_keeps_hash_in_local_paths(tmp_path):
    """A ``#`` in a local path is part of the path, not a ref."""
    parsed = parse_registration_source(f"{tmp_path}/team#1")

    assert parsed.kind == "local"
    assert parsed.url.endswith("/team#1")
    assert parsed.fragment_ref == ""


def test_registration_source_rejects_shorthand_fragment_with_ref():
    with pytest.raises(ValueError, match="one ref source"):
        parse_registration_source("acme/mkt#v1", ref="v2")
