"""The Git config probe error names Git's first diagnostic line and one remedy (#4390)."""

from __future__ import annotations

import pytest

from apm_cli.utils.git_env import GitUrlRewriteProbeError

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-git-child-repository-isolation", "P-LAW-auth.credentials_isolated"),
]

_LIST = "'git config --show-origin --list'"


@pytest.mark.parametrize(
    ("stderr", "remedy"),
    [
        (
            b"fatal: bad config line 3 in file /home/dev/.gitconfig\n",
            f"fix line 3 of '/home/dev/.gitconfig', then check with {_LIST} and retry",
        ),
        (
            b"fatal: bad config variable 'core.x' in file '/etc/gitconfig' at line 7\n",
            f"fix line 7 of '/etc/gitconfig', then check with {_LIST} and retry",
        ),
        (
            b"warning: unable to access '/home/dev/.gitconfig': Is a directory\n"
            b"fatal: error processing config file(s)\n",
            f"fix or remove the config file '/home/dev/.gitconfig', then check with {_LIST} "
            "and retry",
        ),
        (
            b"fatal: '$GIT_DIR' too big\n",
            "the project path is too long for Git; move the project to a shorter path "
            "or run 'git config --global core.longpaths true', then retry",
        ),
        (
            b"error: bogus format in GIT_CONFIG_PARAMETERS\n",
            f"check Git configuration with {_LIST} and retry",
        ),
    ],
)
def test_probe_failure_names_reported_config_and_one_remedy(stderr: bytes, remedy: str) -> None:
    first_line = stderr.decode().splitlines()[0].split(": ", 1)[1]

    message = str(GitUrlRewriteProbeError("Git config probe failed", stderr=stderr))

    assert message == (
        f"Unable to verify Git URL rewrite safety (Git config probe failed: {first_line}); {remedy}"
    )


def test_probe_detail_masks_config_values_and_stays_bounded_ascii() -> None:
    stderr = (
        "fatal: bad numeric config value 'hunter2-secret' for 'http.postbuffer' "
        "in file /home/d\u00e9v/.gitconfig: invalid unit " + "x" * 400
    )

    message = str(GitUrlRewriteProbeError("Git config probe failed", stderr=stderr.encode()))

    assert "hunter2" not in message
    assert "config value '***'" in message
    assert message.isascii()
    assert "x" * 200 not in message
    assert "...); check Git configuration with" in message


@pytest.mark.parametrize(
    "category", ("Git config probe could not start", "Git config probe timed out")
)
def test_probe_categories_without_stderr_keep_wording_and_add_remedy(category: str) -> None:
    message = str(GitUrlRewriteProbeError(category))

    assert message.startswith(
        f"Unable to verify Git URL rewrite safety ({category}); "
        f"check Git configuration and retry; locate each config file with {_LIST}; "
    )
    assert "remove the unsafe rule" not in message
