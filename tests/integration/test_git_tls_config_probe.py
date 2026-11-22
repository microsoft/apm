"""``real_git_tls_config`` read through a real ``git`` child (issue #2545).

The unit module ``tests/unit/utils/test_real_git_tls_config.py`` drives the
owner against a fake ``git config`` child. This module proves the same owner
against real config files and a real ``git``: a global file, then a
command-scope override that clears one key, and the probe never writes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.git_env import real_git_tls_config

pytestmark = [
    pytest.mark.component,
    pytest.mark.windows_compat,
    pytest.mark.promise("P-OWN-git-child-repository-isolation"),
]


def test_reads_real_git_config_files(tmp_path: Path) -> None:
    config = tmp_path / "gitconfig"
    config.write_text(
        "[http]\n\tsslBackend = schannel\n\tsslCAInfo = /global/ca.pem\n", encoding="ascii"
    )
    before = config.read_bytes()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_CONFIG", "XDG_CONFIG_HOME"))
    }
    env.update(
        GIT_CONFIG_GLOBAL=str(config),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="http.sslCAInfo",
        GIT_CONFIG_VALUE_0="",
    )

    assert real_git_tls_config(env) == {"http.sslbackend": "schannel"}
    # The probe only reads: the user's config bytes survive and nothing else is written.
    assert config.read_bytes() == before
    assert sorted(tmp_path.iterdir()) == [config]
