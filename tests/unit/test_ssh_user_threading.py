"""Regression tests for issue #1383 — hardcoded ``git@`` SSH username.

Bug: ``apm install`` always cloned dependencies as ``git@host:...`` even when
the user wrote ``myuser@host:org/repo`` in apm.yml. ``_parse_ssh_url`` captured
the user via regex but discarded it; ``_parse_ssh_protocol_url`` never read
``urlparse().username``; ``build_ssh_url`` hardcoded ``git``.

Fix: thread a validated ``ssh_user`` field from both SSH parsers through
``parse_dependency`` to ``build_ssh_url``. These tests pin:

1. End-to-end parse correctly populates ``ssh_user`` for SCP and ssh:// forms.
2. The supply-chain ``validate_ssh_user`` allowlist rejects option-injection
   payloads (leading ``-``, ``@``, ``/``, ``:``, whitespace, length > 64).
3. Percent-encoded userinfo in ``ssh://`` form is rejected BEFORE urlparse
   decodes it — otherwise ``ssh://%2DoProxyCommand@host/repo`` would smuggle
   ``-oProxyCommand`` past the allowlist.
4. Dependency identity (canonical form, dedup key) stays user-agnostic so a
   project that switches from ``git@`` to a custom user does NOT invalidate
   the lockfile or duplicate cached clones by identity.
5. URL-cache shard key DIFFERS across users so two checkouts with different
   auth contexts never collide on disk.
"""

import pytest

from apm_cli.cache.url_normalize import cache_shard_key
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.utils.github_host import build_ssh_url, validate_ssh_user


class TestValidateSshUser:
    @pytest.mark.parametrize(
        "user",
        [
            "git",
            "myuser",
            "enterprise-user",
            "user_name",
            "u",
            "abc.def",
            "abc+def",
            "user-123",
            "a" * 64,
        ],
    )
    def test_allows_legitimate_users(self, user: str):
        assert validate_ssh_user(user) == user

    @pytest.mark.parametrize(
        "user",
        [
            "-oProxyCommand=evil",  # leading dash = SSH option flag
            "-l",
            ".hidden",  # leading dot is unusual; only alnum/_ allowed at start
            "user@other",  # @ would shift host parsing
            "user/path",  # path traversal-ish
            "user:passwd",  # colon would be parsed as port
            "user name",  # whitespace
            "user\nname",  # CR/LF log/ANSI injection
            "user;rm -rf /",
            "",  # empty
            "a" * 65,  # over length cap
        ],
    )
    def test_rejects_dangerous_users(self, user: str):
        with pytest.raises(ValueError):
            validate_ssh_user(user)

    def test_error_does_not_leak_user_value(self):
        """The error message must not echo the (potentially hostile) user string."""
        with pytest.raises(ValueError) as exc:
            validate_ssh_user("-oProxyCommand=evil")
        # The dangerous string itself must not appear in the message
        # (length disclosure is fine).
        assert "ProxyCommand" not in str(exc.value)
        assert "evil" not in str(exc.value)


class TestParseDependencyPopulatesSshUser:
    def test_scp_shorthand_with_default_git_user(self):
        dep = DependencyReference.parse("git@github.com:acme/repo")
        assert dep.ssh_user == "git"

    def test_scp_shorthand_with_custom_user(self):
        """The reported bug from issue #1383 — myuser must be preserved."""
        dep = DependencyReference.parse("myuser@github.com:acme/repo")
        assert dep.ssh_user == "myuser"

    def test_scp_shorthand_with_emu_user(self):
        dep = DependencyReference.parse("enterprise-user@ghe.corp.com:org/repo")
        assert dep.ssh_user == "enterprise-user"

    def test_ssh_protocol_url_with_default_git_user(self):
        dep = DependencyReference.parse("ssh://git@github.com/acme/repo.git")
        assert dep.ssh_user == "git"

    def test_ssh_protocol_url_with_custom_user(self):
        dep = DependencyReference.parse("ssh://otheruser@github.com/acme/repo.git")
        assert dep.ssh_user == "otheruser"

    def test_ssh_protocol_url_with_user_and_port(self):
        dep = DependencyReference.parse("ssh://buildbot@git.corp.com:7999/team/repo.git")
        assert dep.ssh_user == "buildbot"
        assert dep.port == 7999

    def test_ssh_protocol_url_without_userinfo_defaults_to_git(self):
        dep = DependencyReference.parse("ssh://github.com/acme/repo.git")
        assert dep.ssh_user == "git"

    def test_https_dependency_has_no_ssh_user(self):
        dep = DependencyReference.parse("https://github.com/acme/repo")
        assert dep.ssh_user is None

    def test_shorthand_dependency_has_no_ssh_user(self):
        dep = DependencyReference.parse("acme/repo")
        assert dep.ssh_user is None


class TestSshUserRejectsInjection:
    def test_scp_rejects_leading_dash_user(self):
        """SCP-shorthand: SCP_LIKE_RE already requires alnum/_ at start, but
        validate_ssh_user is the canonical gate; assert end-to-end rejection."""
        with pytest.raises(ValueError):
            DependencyReference.parse("-oProxy@github.com:acme/repo")

    def test_ssh_protocol_rejects_percent_encoded_userinfo(self):
        """CRITICAL: percent-encoded userinfo must NOT smuggle SSH options past
        the allowlist. Two layers of defence:

        1. ``parse()`` calls ``urllib.parse.unquote`` early, so ``%2D`` becomes
           literal ``-`` and then ``validate_ssh_user`` rejects the leading
           dash. End-to-end: any ``ValueError`` is fine.
        2. ``_parse_ssh_protocol_url`` (called directly, bypassing parse()'s
           unquote) ALSO rejects ``%`` in raw userinfo as defence-in-depth --
           see the dedicated direct-call test below.
        """
        with pytest.raises(ValueError):
            DependencyReference.parse("ssh://%2DoProxyCommand@github.com/acme/repo.git")

    def test_ssh_protocol_rejects_percent_encoded_userinfo_lowercase(self):
        with pytest.raises(ValueError):
            DependencyReference.parse("ssh://%2doProxyCommand@github.com/acme/repo.git")

    def test_parse_ssh_protocol_url_direct_rejects_percent_encoding(self):
        """Defence-in-depth: even if ``parse()``'s early unquote were removed
        or bypassed, ``_parse_ssh_protocol_url`` itself rejects percent-encoded
        userinfo before urlparse can decode it."""
        with pytest.raises(ValueError, match="Percent-encoded"):
            DependencyReference._parse_ssh_protocol_url(
                "ssh://%2DoProxyCommand@github.com/acme/repo.git"
            )

    def test_ssh_protocol_rejects_overlong_user(self):
        long_user = "a" * 65
        with pytest.raises(ValueError):
            DependencyReference.parse(f"ssh://{long_user}@github.com/acme/repo.git")


class TestSshUserDoesNotAffectIdentity:
    """Dependency identity must stay user-agnostic. The user is auth context,
    not part of what package is being installed. Otherwise:

    - Switching from ``git@`` to ``myuser@`` would silently dedupe to a new
      lockfile entry (looks like a different package).
    - Two devs on the same project with different SSH usernames would write
      conflicting lockfiles.
    """

    def test_to_canonical_identical_across_users(self):
        a = DependencyReference.parse("git@github.com:acme/repo")
        b = DependencyReference.parse("myuser@github.com:acme/repo")
        assert a.to_canonical() == b.to_canonical()

    def test_get_identity_identical_across_users(self):
        a = DependencyReference.parse("git@github.com:acme/repo")
        b = DependencyReference.parse("myuser@github.com:acme/repo")
        assert a.get_identity() == b.get_identity()

    def test_to_canonical_identical_for_ssh_protocol_users(self):
        a = DependencyReference.parse("ssh://git@github.com/acme/repo.git")
        b = DependencyReference.parse("ssh://other@github.com/acme/repo.git")
        assert a.to_canonical() == b.to_canonical()


class TestSshUserDifferentiatesCacheShard:
    """Cache shard key MUST differ across users -- different auth context can
    resolve to different refs (e.g. private fork accessible only to one user)
    so checkouts must not share an on-disk directory."""

    def test_cache_shard_key_differs_across_users(self):
        key_git = cache_shard_key("git@github.com:acme/repo")
        key_user = cache_shard_key("myuser@github.com:acme/repo")
        assert key_git != key_user

    def test_cache_shard_key_stable_for_same_user(self):
        key1 = cache_shard_key("myuser@github.com:acme/repo")
        key2 = cache_shard_key("myuser@github.com:acme/repo")
        assert key1 == key2


class TestBuildSshUrlDispatchesUser:
    """Round-trip: parsed dep -> backend -> SSH URL with the right user."""

    def test_round_trip_custom_user_scp(self):
        dep = DependencyReference.parse("myuser@github.com:acme/repo")
        url = build_ssh_url(dep.host, dep.repo_url, port=dep.port, user=dep.ssh_user or "git")
        assert url == "myuser@github.com:acme/repo.git"

    def test_round_trip_custom_user_with_port(self):
        dep = DependencyReference.parse("ssh://buildbot@git.corp.com:7999/team/repo.git")
        url = build_ssh_url(dep.host, dep.repo_url, port=dep.port, user=dep.ssh_user or "git")
        assert url == "ssh://buildbot@git.corp.com:7999/team/repo.git"

    def test_repository_cache_url_keeps_explicit_ssh_context(self):
        dep = DependencyReference.parse("ssh://buildbot@git.corp.com:7999/Group/Subgroup/Repo.git")
        assert (
            dep.to_repository_cache_url()
            == "ssh://buildbot@git.corp.com:7999/Group/Subgroup/Repo.git"
        )
