"""Tests for skill transformer functionality (pure logic cases)."""

import pytest

from apm_cli.integration.skill_transformer import to_hyphen_case

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mypackage", "mypackage"),
        ("myPackage", "my-package"),
        ("MyPackage", "my-package"),
        ("my_package", "my-package"),
        ("Brand Guidelines", "brand-guidelines"),
        ("my_AwesomePackage", "my-awesome-package"),
        ("my@package!name", "mypackagename"),
        ("my--package", "my-package"),
        ("-mypackage-", "mypackage"),
    ],
    ids=[
        "basic-lowercase",
        "camel-case",
        "pascal-case",
        "underscores",
        "spaces",
        "mixed-separators",
        "invalid-characters",
        "consecutive-hyphens",
        "leading-trailing-hyphens",
    ],
)
def test_to_hyphen_case(raw, expected):
    """to_hyphen_case normalizes names to lowercase hyphen-separated slugs."""
    assert to_hyphen_case(raw) == expected


# NOTE: TestAgentIntegratorSkillSupport class has been REMOVED as part of T5.
#
# The find_skill_file() method was removed from AgentIntegrator because:
# - Skills are NO LONGER transformed to .agent.md files
# - Skills now go directly to .github/skills/ via SkillIntegrator
# - See skill-strategy.md for the full architectural rationale
