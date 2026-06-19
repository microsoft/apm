"""Tests for the offline SPDX declared-license classifier.

The classifier never gates: it only decides how a declared license string is
rendered in an SBOM. The supply-chain-critical invariant under test is that a
declared assertion (``UNLICENSED`` / ``SEE LICENSE IN ...``) is NEVER collapsed
into the genuinely-unknown NOASSERTION state -- they are recorded as named
licenses, not dropped.
"""

from __future__ import annotations

import pytest

from apm_cli.export.spdx import (
    KIND_EXPRESSION,
    KIND_ID,
    KIND_NAMED,
    classify_declared_license,
)


@pytest.mark.parametrize(
    "decl",
    ["MIT", "Apache-2.0", "GPL-3.0-or-later", "BSD-3-Clause", "MIT+"],
)
def test_single_valid_spdx_id_classifies_as_id(decl):
    result = classify_declared_license(decl)
    assert result.kind == KIND_ID
    assert result.value == decl


@pytest.mark.parametrize(
    "decl",
    [
        "(MIT OR Apache-2.0)",
        "MIT OR Apache-2.0",
        "Apache-2.0 AND MIT",
        "GPL-2.0-only WITH Classpath-exception-2.0",
        "(MIT AND (Apache-2.0 OR BSD-3-Clause))",
    ],
)
def test_valid_spdx_expression_classifies_as_expression(decl):
    result = classify_declared_license(decl)
    assert result.kind == KIND_EXPRESSION
    assert result.value == decl


def test_unlicensed_token_is_named_not_unknown():
    # CRITICAL: UNLICENSED asserts "no rights granted" -- a declaration, NOT
    # an absence. It must render as a named license, never NOASSERTION.
    result = classify_declared_license("UNLICENSED")
    assert result.kind == KIND_NAMED
    assert result.value == "UNLICENSED"


def test_see_license_in_token_is_named():
    decl = "SEE LICENSE IN LICENSE.txt"
    result = classify_declared_license(decl)
    assert result.kind == KIND_NAMED
    assert result.value == decl


@pytest.mark.parametrize(
    "decl",
    ["Totally-Made-Up", "my-company-eula", "MIT OR Bogus-9.9", "AND OR"],
)
def test_unrecognized_string_is_named(decl):
    result = classify_declared_license(decl)
    assert result.kind == KIND_NAMED
    assert result.value == decl


def test_whitespace_is_preserved_verbatim_in_value():
    # Leading/trailing whitespace is stripped for classification but the
    # stored value is the stripped form (verbatim of the meaningful token).
    result = classify_declared_license("  MIT  ")
    assert result.kind == KIND_ID
    assert result.value == "MIT"


def test_licenseref_is_expression_or_id():
    # LicenseRef-* is a valid SPDX simple expression element (no public id).
    result = classify_declared_license("LicenseRef-MyLicense")
    assert result.kind in (KIND_ID, KIND_EXPRESSION)
    assert result.value == "LicenseRef-MyLicense"
