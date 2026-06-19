"""Registry conformance tests -- sec.4.2.3, sec.11.3.3.

req-rg-001 (trust anchor): the SHA-256 of the archive bytes the
Registry serves MUST equal the digest the Registry advertises for
that version. This is the ONE substantive normative statement v0.1
places on Registry implementations; the rest is reserved for v0.2.

The fixture `integrity/security-baseline-2.3.1.tar.gz` simulates
a Registry's published archive; its paired
`security-baseline-2.3.1.frozen.yaml` declares the advertised
digest. The test reads the archive bytes, recomputes the SHA-256,
and asserts equality with the declared digest. Tampering with
either side breaks the bind.
"""

from __future__ import annotations

import pytest

from tests.spec_conformance._helpers import (
    assert_spec_contains,
    fixture_path,
    load_yaml_fixture,
    sha256_hex,
)


@pytest.mark.req("req-rg-001")
def test_registry_trust_anchor_archive_bytes_match_advertised_digest():
    """Active trust-anchor invariant test.

    Reads the committed simulated-Registry archive, computes its
    SHA-256, and asserts that the digest the lockfile records (the
    digest a conforming Registry would advertise) matches the bytes.
    """
    arc_bytes = fixture_path("integrity", "security-baseline-2.3.1.tar.gz").read_bytes()
    actual = "sha256:" + sha256_hex(arc_bytes)
    doc = load_yaml_fixture("integrity", "security-baseline-2.3.1.frozen.yaml")
    advertised = doc["dependencies"][0]["resolved_hash"]
    assert actual == advertised, (
        "Trust-anchor invariant broken: archive bytes hash to "
        f"{actual!r} but lockfile advertises {advertised!r}. "
        "Either the committed archive bytes drifted, or the fixture's "
        "advertised hash was edited without recomputing."
    )
    # And the spec language carrying the invariant MUST persist.
    assert_spec_contains(
        "SHA-256 of those bytes equals the digest the\nRegistry advertises",
        "MUST NOT mutate previously",
        # Publish-side idempotency clause (round-3 fold):
        "(idempotent republish)",
        "A Registry MUST\nNOT replace the bytes",
    )
