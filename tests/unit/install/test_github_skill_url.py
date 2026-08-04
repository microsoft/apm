from __future__ import annotations

import pytest

from apm_cli.install.package_resolution import (
    normalize_github_skill_url_package,
    normalize_github_skill_url_packages,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/owner/repo/blob/main/skills/review/SKILL.md#L1",
            ("https://github.com/owner/repo#main", ("review",)),
        ),
        (
            "https://raw.githubusercontent.com/owner/repo/v1.2.3/"
            "skills/productivity/handoff/SKILL.md?raw=1",
            (
                "https://github.com/owner/repo#v1.2.3",
                ("productivity/handoff",),
            ),
        ),
    ],
)
def test_normalize_github_skill_url_package(url, expected):
    assert normalize_github_skill_url_package(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "owner/repo",
        "https://example.com/owner/repo/blob/main/skills/review/SKILL.md",
        "https://github.com/owner/repo",
    ],
)
def test_normalize_github_skill_url_package_ignores_other_references(url):
    assert normalize_github_skill_url_package(url) is None


@pytest.mark.parametrize(
    ("url", "message"),
    [
        (
            "https://github.com/owner/repo/blob/main/skills/review/README.md",
            "must point to a SKILL.md file",
        ),
        (
            "https://github.com/owner/repo/blob/main/docs/SKILL.md",
            "must point inside a `skills/` directory",
        ),
        (
            "https://github.com/owner/repo/blob/skills/review/SKILL.md",
            "must include a branch, tag, or commit",
        ),
        (
            "https://github.com/owner/repo/blob/main/skills/SKILL.md",
            "must include a skill name",
        ),
        (
            "https://github.com/owner/repo/blob/main/skills/%2E%2E/SKILL.md",
            "traversal sequence",
        ),
    ],
)
def test_normalize_github_skill_url_package_rejects_malformed_paths(url, message):
    with pytest.raises(ValueError, match=message):
        normalize_github_skill_url_package(url)


def test_normalize_github_skill_url_packages_combines_per_repo_skills():
    packages, skill_subsets, invalid = normalize_github_skill_url_packages(
        [
            "https://github.com/owner/repo/blob/main/skills/review/SKILL.md",
            "https://github.com/owner/repo/blob/main/skills/productivity/handoff/SKILL.md",
            "other/package",
        ]
    )

    assert packages == ("https://github.com/owner/repo#main", "other/package")
    assert skill_subsets == {
        "https://github.com/owner/repo#main": ("productivity/handoff", "review")
    }
    assert invalid == []
