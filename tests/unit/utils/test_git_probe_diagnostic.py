"""Git config probes: the error names Git's first diagnostic line and one remedy (#4390),
and one command probes each effective config once, not once per dependency.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from apm_cli.core.invocation_memo import forget_invocation_memos, invocation_scope
from apm_cli.utils.git_env import (
    GitConfigEntry,
    GitUrlRewriteProbeError,
    configured_git_url_policy,
    git_clone_env,
    real_git_tls_config,
)

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


def test_probe_detail_masks_a_config_value_that_contains_apostrophes() -> None:
    stderr = b"fatal: bad boolean config value 'prefix'private-suffix' for 'core.bare'\n"

    message = str(GitUrlRewriteProbeError("Git config probe failed", stderr=stderr))

    assert "private-suffix" not in message
    assert "config value '***' for 'core.bare'" in message


def test_probe_remedy_never_names_a_truncated_config_path() -> None:
    path = "/home/dev/" + "nested/" * 40 + ".gitconfig"
    stderr = f"fatal: bad config line 3 in file {path}\n".encode()

    message = str(GitUrlRewriteProbeError("Git config probe failed", stderr=stderr))

    assert message.endswith(f"...); check Git configuration with {_LIST} and retry")
    assert "fix line" not in message


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


_LISTING = b"global\0protocol.file.allow\nalways\0command\0http.extraheader\n\0"
_RESET_HEADER = GitConfigEntry("command", "http.extraheader", "")
_ANON = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader", "GIT_CONFIG_VALUE_0": ""}


class _GitChild:
    """A finished child process as ``subprocess.run`` and the probe runner read it."""

    def __init__(self, command: list[str], returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.args = command
        self.returncode = returncode
        self._output = (stdout, stderr)

    def __enter__(self) -> _GitChild:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def communicate(self, input=None, timeout=None) -> tuple[bytes, bytes]:
        return self._output

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout=None) -> int:
        return self.returncode

    def kill(self) -> None:
        return None


class _GitChildren:
    """Stand in for every Git child process; each start is recorded."""

    def __init__(self, listing: bytes = _LISTING) -> None:
        self.listing = listing
        self.failures = 0
        self.commands: list[list[str]] = []

    def __call__(self, command, **_kwargs) -> _GitChild:
        command = [str(arg) for arg in command]
        self.commands.append(command)
        if "--get-urlmatch" in command:
            return _GitChild(command, 0, b"X-Apm-Config-Probe: 0\0", b"")
        if "--list" in command and self.failures:
            self.failures -= 1
            return _GitChild(command, 128, b"", b"fatal: bad config line 1\n")
        if "--list" in command:
            return _GitChild(command, 0, self.listing, b"")
        if "init" in command:
            # `git init --bare <target>` writes the config the clone probe checks for.
            target = Path(command[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "config").write_text("[core]\n\tbare = true\n", encoding="utf-8")
        return _GitChild(command, 0, b"", b"")

    def count(self, flag: str) -> int:
        return sum(flag in command for command in self.commands)

    def config_probes_in_a_git_dir(self) -> int:
        return sum(
            "config" in command and any(arg.startswith("--git-dir") for arg in command)
            for command in self.commands
        )


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> _GitChildren:
    children = _GitChildren()
    monkeypatch.setattr(subprocess, "Popen", children)
    return children


def _clone_env(tmp_path: Path, index: int) -> dict[str, str]:
    url = f"https://github.com/perf-org/pkg-{index:03d}"
    return git_clone_env(url, dict(_ANON), tmp_path / "staged" / str(index), bare=True)


def test_dependency_clones_probe_config_once_and_urlmatch_once_per_url(
    probes: _GitChildren, tmp_path: Path
) -> None:
    with invocation_scope():
        envs = [_clone_env(tmp_path, index) for index in (0, 1, 2, 0)]

    # One probe for the ambient parent config, one for the clone child env.
    assert probes.count("--list") == 2
    assert probes.count("--get-urlmatch") == 3
    assert probes.config_probes_in_a_git_dir() == 0
    assert envs[0] == envs[3]
    assert (envs[0]["GIT_CONFIG_KEY_0"], envs[0]["GIT_CONFIG_VALUE_0"]) == (
        "protocol.file.allow",
        "always",
    )


def test_conditional_include_keeps_probing_each_fresh_clone_directory(
    probes: _GitChildren, tmp_path: Path
) -> None:
    probes.listing = _LISTING + b"global\0includeif.gitdir:~/corp/.path\ncorp.gitconfig\0"

    with invocation_scope():
        for index in range(3):
            _clone_env(tmp_path, index)

    assert probes.config_probes_in_a_git_dir() == 6


@pytest.mark.parametrize("variable", ["GIT_CONFIG_GLOBAL", "HOME", "XDG_CONFIG_HOME"])
def test_relative_config_location_keeps_probing_each_fresh_clone_directory(
    probes: _GitChildren, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    # A relative location resolves against the probe cwd, which differs per directory.
    monkeypatch.setenv(variable, "relative/gitconfig")

    with invocation_scope():
        for index in range(3):
            url = f"https://github.com/perf-org/pkg-{index:03d}"
            overrides = {**_ANON, variable: "relative/gitconfig"}
            git_clone_env(url, overrides, tmp_path / "staged" / str(index), bare=True)

    assert probes.config_probes_in_a_git_dir() == 6


@pytest.mark.parametrize("variable", ["GIT_CONFIG_GLOBAL", "HOME", "XDG_CONFIG_HOME"])
def test_changed_config_input_probes_again(probes: _GitChildren, variable: str) -> None:
    env = {"HOME": "/home/a", "GIT_CONFIG_GLOBAL": "/home/a/.gitconfig"}

    with invocation_scope():
        for _ in range(3):
            real_git_tls_config(env)
            configured_git_url_policy(env)
        assert probes.count("--list") == 1
        configured_git_url_policy({**env, variable: "/elsewhere"})
        assert probes.count("--list") == 2
        forget_invocation_memos()
        configured_git_url_policy(env)

    assert probes.count("--list") == 3


def test_failed_config_probe_is_retried_not_reused(probes: _GitChildren) -> None:
    probes.failures = 1

    with invocation_scope():
        with pytest.raises(GitUrlRewriteProbeError, match="bad config line 1"):
            configured_git_url_policy({})
        assert configured_git_url_policy({}) == ((), (_RESET_HEADER,))

    assert probes.count("--list") == 2
