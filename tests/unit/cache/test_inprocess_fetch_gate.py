"""Which git config and attributes keep the in-process fetch equal to git (W3d W1-W4).

Git for Windows ships a system config with a credential helper, git-lfs
filters, autocrlf and an sslBackend/sslCAInfo pair, plus a system
gitattributes file of diff drivers. None of them changes git's anonymous
fetch or (under APM's -c pins) its checkout bytes, so the gate must stay
open for them; every key that would change git's request must close it,
naming the key.
"""

from __future__ import annotations

import contextlib
import os
from collections import namedtuple
from pathlib import Path

import pytest

from apm_cli.cache import inprocess_fetch as fetch

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-git-transport-selection")]

Entry = namedtuple("Entry", "key value")

GIT_FOR_WINDOWS_SYSTEM = [
    Entry("diff.astextplain.textconv", "astextplain"),
    Entry("filter.lfs.clean", "git-lfs clean -- %f"),
    Entry("filter.lfs.smudge", "git-lfs smudge -- %f"),
    Entry("filter.lfs.process", "git-lfs filter-process"),
    Entry("filter.lfs.required", "true"),
    Entry("http.sslbackend", "openssl"),
    Entry("http.sslcainfo", "C:/Program Files/Git/mingw64/etc/ssl/certs/ca-bundle.crt"),
    Entry("core.autocrlf", "true"),
    Entry("core.fscache", "true"),
    Entry("core.symlinks", "false"),
    Entry("pull.rebase", "false"),
    Entry("credential.helper", "manager"),
    Entry("credential.https://dev.azure.com.usehttppath", "true"),
    Entry("init.defaultbranch", "main"),
]

GIT_FOR_WINDOWS_ATTRIBUTES = """\
*.c\tdiff=cpp
*.h\tdiff=cpp
*.py\tdiff=python
*.doc\tdiff=astextplain
*.DOC\tdiff=astextplain
*.pdf\tdiff=astextplain
*.rtf\tdiff=astextplain
"""


def test_git_for_windows_system_config_keeps_the_gate_open() -> None:
    values, prefixes = fetch._classify(GIT_FOR_WINDOWS_SYSTEM)

    assert values["http.sslbackend"] == "openssl"
    assert prefixes == ()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("http.proactiveAuth", "basic"),
        ("http.https://github.com/.extraHeader", "Authorization: Bearer x"),
        ("http.proxy", "http://proxy.invalid:8080"),
        ("http.sslVerify", "false"),
        ("http.schannelUseSSLCAInfo", "true"),
        ("core.gitProxy", "proxy-command"),
        ("core.askPass", "askpass"),
        ("protocol.allow", "never"),
    ],
)
def test_each_key_that_changes_gits_request_closes_the_gate_by_name(key: str, value: str) -> None:
    with pytest.raises(fetch._Refuse, match=key):
        fetch._classify([*GIT_FOR_WINDOWS_SYSTEM, Entry(key, value)])


def test_an_insteadof_rewrite_of_the_url_is_reported_for_the_url_check() -> None:
    _values, prefixes = fetch._classify(
        [Entry("url.https://mirror.invalid/.insteadOf", "https://github.com/")]
    )

    assert prefixes == ("https://github.com/",)


def test_schannel_trusts_the_os_store_only_on_windows(tmp_path: Path) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("x", encoding="utf-8")

    if os.name == "nt":
        assert fetch._trust({}, {"http.sslbackend": "schannel"}) == ("os", "")
        with pytest.raises(fetch._Refuse):
            fetch._trust({"GIT_SSL_CAINFO": str(ca)}, {"http.sslbackend": "schannel"})
    else:
        with pytest.raises(fetch._Refuse, match="sslBackend"):
            fetch._trust({}, {"http.sslbackend": "schannel"})
    with pytest.raises(fetch._Refuse, match="sslBackend"):
        fetch._trust({}, {"http.sslbackend": "gnutls"})


def test_openssl_trusts_exactly_gits_ca_file(tmp_path: Path) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("x", encoding="utf-8")

    assert fetch._trust({"GIT_SSL_CAINFO": str(ca)}, {"http.sslbackend": "openssl"}) == (
        "file",
        str(ca),
    )


@pytest.mark.skipif(os.name != "nt", reason="Git for Windows' curl bundle is not certifi")
def test_openssl_on_windows_without_a_named_ca_file_falls_back() -> None:
    with pytest.raises(fetch._Refuse, match="CA file"):
        fetch._trust({}, {"http.sslbackend": "openssl"})


@pytest.mark.parametrize("key", ["transfer.fsckObjects", "fetch.fsckObjects"])
def test_fsck_on_receive_keeps_git(key: str) -> None:
    with pytest.raises(fetch._Refuse, match=key):
        fetch._classify([*GIT_FOR_WINDOWS_SYSTEM, Entry(key, "true")])
    fetch._classify([*GIT_FOR_WINDOWS_SYSTEM, Entry(key, "false")])


def test_git_for_windows_attributes_are_inert(tmp_path: Path) -> None:
    path = tmp_path / "gitattributes"
    path.write_text(GIT_FOR_WINDOWS_ATTRIBUTES, encoding="utf-8")

    assert fetch._inert_attributes(str(path))


@pytest.mark.parametrize(
    "line",
    [
        "* text=auto",
        "*.md eol=crlf",
        "*.sh -text",
        "*.bin filter=lfs diff=lfs merge=lfs",
        "*.c ident",
        "*.txt working-tree-encoding=UTF-16",
        "*.x crlf",
        "[attr]mine text",
        '"a b" diff',
        "*.y unknown-attr",
    ],
)
def test_any_checkout_affecting_or_unknown_attribute_refuses(tmp_path: Path, line: str) -> None:
    path = tmp_path / "gitattributes"
    path.write_text(GIT_FOR_WINDOWS_ATTRIBUTES + line + "\n", encoding="utf-8")

    assert not fetch._inert_attributes(str(path))


class _Response:
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status_code = status
        self.headers = headers

    def iter_content(self, _size: int):
        yield b"0000"


def test_a_401_challenge_falls_back_without_sending_credentials(monkeypatch) -> None:
    sent: dict = {}

    @contextlib.contextmanager
    def fake_request(session, method, url, **kwargs):
        sent.update(kwargs)
        yield _Response(401, {"WWW-Authenticate": 'Basic realm="GitHub"'})

    monkeypatch.setattr("apm_cli.utils.http_credentials.origin_bounded_request", fake_request)
    monkeypatch.setattr(fetch, "_session", lambda trust: object())

    with pytest.raises(fetch._Refuse, match="auth challenge"):
        fetch._post("https://github.com/o/r/git-upload-pack", None, ("os", ""), b"0000")

    assert "Authorization" not in sent["headers"]
    assert sent["ambient_credentials"] is False
    assert sent["max_hops"] == 0


def _frozen_from(config: Path):
    """A stand-in for git_network_env: freezes the config file's entries into env."""
    built: list[str] = []

    def fake_network_env(url, env):
        built.append(url)
        frozen = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        section = ""
        entries = []
        for line in config.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line.strip("[]").split()[0].lower()
            elif "=" in line:
                key, value = (part.strip() for part in line.split("=", 1))
                entries.append((f"{section}.{key}", value))
        frozen["GIT_CONFIG_COUNT"] = str(len(entries))
        for index, (key, value) in enumerate(entries):
            frozen[f"GIT_CONFIG_KEY_{index}"] = key
            frozen[f"GIT_CONFIG_VALUE_{index}"] = value
        return frozen

    return built, fake_network_env


@pytest.fixture
def frozen_world(tmp_path: Path, monkeypatch):
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = a\n", encoding="utf-8")
    built, fake = _frozen_from(config)
    monkeypatch.setattr("apm_cli.utils.git_env.git_network_env", fake)
    monkeypatch.setattr(fetch, "_config_paths", lambda env: (str(config),))
    monkeypatch.setattr(fetch, "_binary_identity", lambda git: (git, 1, 2, 3, 4))
    return config, built


def test_the_frozen_network_env_is_reused_only_while_nothing_it_read_changed(frozen_world):
    from apm_cli.core.invocation_memo import invocation_scope

    config, built = frozen_world
    env = {"HOME": "/home/a"}

    with invocation_scope():
        first = fetch._network_env(env)
        assert fetch._network_env(env) == first
        assert len(built) == 1

        fetch._network_env({**env, "HOME": "/home/b"})
        assert len(built) == 2

        config.write_text("[user]\n\tname = b\n", encoding="utf-8")
        fetch._network_env(env)
        assert len(built) == 3

        config.write_text("[include]\n\tpath = other\n", encoding="utf-8")
        fetch._network_env(env)
        fetch._network_env(env)
        assert len(built) == 5


def test_an_extra_header_added_mid_command_falls_back(frozen_world):
    from apm_cli.core.invocation_memo import invocation_scope

    config, _built = frozen_world
    env = {"HOME": "/home/a"}

    with invocation_scope():
        fetch._effective(env)
        config.write_text("[http]\n\textraHeader = Authorization: x\n", encoding="utf-8")
        with pytest.raises(fetch._Refuse, match="extraHeader"):
            fetch._effective(env)
