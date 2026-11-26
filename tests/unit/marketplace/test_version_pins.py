"""Tests for ref pin cache (immutability advisory).

Covers:
- Loading from missing / corrupt / valid pin files
- Recording and persisting pins
- Detecting ref changes (possible ref swap)
- Multi-plugin isolation
- Atomic write via os.replace
"""

import json
import os
import re
from unittest.mock import patch

import pytest

from apm_cli.marketplace.version_pins import (
    RefPinStoreError,
    _pin_key,
    _pins_path,
    check_ref_pin,
    load_ref_pins,
    record_ref_pin,
    save_ref_pins,
)

# ---------------------------------------------------------------------------
# Unit tests -- load / save
# ---------------------------------------------------------------------------


class TestLoadRefPins:
    """Loading the pin file from disk."""

    def test_load_empty_no_file(self, tmp_path):
        """Missing file returns empty dict."""
        result = load_ref_pins(pins_dir=str(tmp_path))
        assert result == {}

    def test_load_missing_no_warning_by_default(self, tmp_path, caplog):
        """Missing file does NOT warn when expect_exists is False (default)."""
        import logging

        with caplog.at_level(logging.WARNING):
            result = load_ref_pins(pins_dir=str(tmp_path))
        assert result == {}
        assert "expected but missing" not in caplog.text

    def test_load_missing_warns_when_expected(self, tmp_path, caplog):
        """Missing file warns when expect_exists=True."""
        import logging

        with caplog.at_level(logging.WARNING):
            result = load_ref_pins(pins_dir=str(tmp_path), expect_exists=True)
        assert result == {}
        assert "expected but missing" in caplog.text
        assert "ref-swap detection is disabled" in caplog.text

    @pytest.mark.parametrize("content", ["{not valid json!!!", '["a list", "not a dict"]'])
    def test_an_unreadable_pins_file_fails_closed(self, tmp_path, content):
        """Corrupt or non-object JSON refuses instead of disabling detection."""
        path = tmp_path / "version-pins.json"
        path.write_text(content)
        with pytest.raises(RefPinStoreError, match="delete it to start a new record"):
            load_ref_pins(pins_dir=str(tmp_path))
        assert path.read_text() == content

    def test_load_valid(self, tmp_path):
        """Valid JSON is returned as-is."""
        data = {"mkt/plug": "abc123"}
        path = tmp_path / "version-pins.json"
        path.write_text(json.dumps(data))
        result = load_ref_pins(pins_dir=str(tmp_path))
        assert result == data


class TestSaveRefPins:
    """Saving the pin file to disk."""

    def test_save_creates_file(self, tmp_path):
        """Save creates the file if it does not exist."""
        pins = {"mkt/plug": "ref1"}
        save_ref_pins(pins, pins_dir=str(tmp_path))

        path = tmp_path / "version-pins.json"
        assert path.exists()
        assert json.loads(path.read_text()) == pins

    def test_save_creates_parent_dirs(self, tmp_path):
        """Save creates intermediate directories if needed."""
        nested = tmp_path / "a" / "b"
        pins = {"mkt/plug": "ref2"}
        save_ref_pins(pins, pins_dir=str(nested))

        path = nested / "version-pins.json"
        assert path.exists()
        assert json.loads(path.read_text()) == pins


# ---------------------------------------------------------------------------
# Unit tests -- record / check
# ---------------------------------------------------------------------------


class TestRecordAndCheck:
    """Recording pins and checking for ref changes."""

    def test_record_and_load(self, tmp_path):
        """Record a pin and verify it persists on disk."""
        record_ref_pin("mkt", "plug", "sha-aaa", pins_dir=str(tmp_path))
        pins = load_ref_pins(pins_dir=str(tmp_path))
        assert pins["mkt/plug"] == "sha-aaa"

    def test_check_new_pin(self, tmp_path):
        """First time seeing a plugin returns None (no warning)."""
        result = check_ref_pin("mkt", "plug", "sha-aaa", pins_dir=str(tmp_path))
        assert result is None

    def test_check_matching_pin(self, tmp_path):
        """Same ref as previously recorded returns None."""
        record_ref_pin("mkt", "plug", "sha-aaa", pins_dir=str(tmp_path))
        result = check_ref_pin("mkt", "plug", "sha-aaa", pins_dir=str(tmp_path))
        assert result is None

    def test_check_changed_pin(self, tmp_path):
        """Different ref returns the previous (old) ref string."""
        record_ref_pin("mkt", "plug", "sha-aaa", pins_dir=str(tmp_path))
        result = check_ref_pin("mkt", "plug", "sha-bbb", pins_dir=str(tmp_path))
        assert result == "sha-aaa"

    def test_record_overwrites(self, tmp_path):
        """Recording the same plugin twice overwrites the old ref."""
        record_ref_pin("mkt", "plug", "sha-aaa", pins_dir=str(tmp_path))
        record_ref_pin("mkt", "plug", "sha-bbb", pins_dir=str(tmp_path))
        pins = load_ref_pins(pins_dir=str(tmp_path))
        assert pins["mkt/plug"] == "sha-bbb"

    def test_multiple_plugins(self, tmp_path):
        """Different plugins do not interfere with each other."""
        record_ref_pin("mkt", "alpha", "ref-a", pins_dir=str(tmp_path))
        record_ref_pin("mkt", "beta", "ref-b", pins_dir=str(tmp_path))

        assert check_ref_pin("mkt", "alpha", "ref-a", pins_dir=str(tmp_path)) is None
        assert check_ref_pin("mkt", "beta", "ref-b", pins_dir=str(tmp_path)) is None
        # Alpha ref changed, beta unchanged
        assert check_ref_pin("mkt", "alpha", "ref-x", pins_dir=str(tmp_path)) == "ref-a"
        assert check_ref_pin("mkt", "beta", "ref-b", pins_dir=str(tmp_path)) is None

    def test_version_scoped_pins_do_not_conflict(self, tmp_path):
        """Different versions of the same plugin get independent pins."""
        record_ref_pin("mkt", "plug", "sha-aaa", version="1.0.0", pins_dir=str(tmp_path))
        record_ref_pin("mkt", "plug", "sha-bbb", version="2.0.0", pins_dir=str(tmp_path))

        # Each version has its own pin
        assert (
            check_ref_pin("mkt", "plug", "sha-aaa", version="1.0.0", pins_dir=str(tmp_path)) is None
        )
        assert (
            check_ref_pin("mkt", "plug", "sha-bbb", version="2.0.0", pins_dir=str(tmp_path)) is None
        )

        # Changing v1's ref flags it without affecting v2
        assert (
            check_ref_pin("mkt", "plug", "sha-xxx", version="1.0.0", pins_dir=str(tmp_path))
            == "sha-aaa"
        )
        assert (
            check_ref_pin("mkt", "plug", "sha-bbb", version="2.0.0", pins_dir=str(tmp_path)) is None
        )


# ---------------------------------------------------------------------------
# Unit tests -- key normalization
# ---------------------------------------------------------------------------


class TestPinKey:
    """Pin key construction and normalization."""

    def test_lowercase(self):
        assert _pin_key("MKT", "Plugin") == "mkt/plugin"

    def test_already_lower(self):
        assert _pin_key("mkt", "plugin") == "mkt/plugin"

    def test_with_version(self):
        assert _pin_key("MKT", "Plugin", "2.1.0") == "mkt/plugin/2.1.0"

    def test_empty_version_omits_segment(self):
        assert _pin_key("mkt", "plugin", "") == "mkt/plugin"


# ---------------------------------------------------------------------------
# Unit tests -- pins_path
# ---------------------------------------------------------------------------


class TestPinsPath:
    """Path construction for the pins file."""

    def test_custom_dir(self, tmp_path):
        result = _pins_path(pins_dir=str(tmp_path))
        assert result == os.path.join(str(tmp_path), "version-pins.json")

    def test_default_dir_follows_apm_home_at_call_time(self, tmp_path, monkeypatch):
        """Default path (no pins_dir) is under the APM_HOME in effect for the call."""
        monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
        result = _pins_path(pins_dir=None)
        assert result == os.path.join(
            str(tmp_path / "home"), "cache", "marketplace", "version-pins.json"
        )


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    """Verify save uses atomic write pattern (tmp + os.replace)."""

    def test_atomic_write_uses_replace(self, tmp_path):
        """os.replace is called to atomically move the temp file."""
        pins = {"mkt/plug": "ref1"}

        with patch("os.replace", wraps=os.replace) as mock_replace:
            save_ref_pins(pins, pins_dir=str(tmp_path))
            mock_replace.assert_called_once()
            args = mock_replace.call_args[0]
            assert args[0].endswith(".tmp")
            assert args[1].endswith("version-pins.json")

    def test_no_tmp_file_remains(self, tmp_path):
        """After save, no .tmp file should remain on disk."""
        save_ref_pins({"k": "r"}, pins_dir=str(tmp_path))
        remaining = list(tmp_path.iterdir())
        assert all(not f.name.endswith(".tmp") for f in remaining)


# ---------------------------------------------------------------------------
# Fail-open behavior
# ---------------------------------------------------------------------------


class TestFailOpen:
    """Advisory system must never raise on I/O errors."""

    def test_save_to_readonly_dir_does_not_raise(self, tmp_path):
        """Save to an unwritable location logs and returns without error."""
        bad_dir = "/dev/null/impossible"
        save_ref_pins({"k": "r"}, pins_dir=bad_dir)

    def test_check_with_corrupt_file_refuses(self, tmp_path):
        """check_ref_pin with a corrupt file refuses the install."""
        path = tmp_path / "version-pins.json"
        path.write_text("CORRUPT!!!")
        with pytest.raises(RefPinStoreError, match=re.escape(str(path))):
            check_ref_pin("mkt", "plug", "ref", pins_dir=str(tmp_path))

    def test_check_with_non_string_plugin_entry(self, tmp_path):
        """If the plugin entry is not a string, return None gracefully."""
        data = {"mkt/plug": {"nested": "dict"}}
        path = tmp_path / "version-pins.json"
        path.write_text(json.dumps(data))
        result = check_ref_pin("mkt", "plug", "ref", pins_dir=str(tmp_path))
        assert result is None


def test_an_unreadable_pins_file_refuses_a_marketplace_resolution(tmp_path):
    """The resolver's pin gate refuses rather than staging an unchecked ref."""
    from apm_cli.marketplace.version_pins import enforce_ref_pin, open_ref_pin_stage

    path = tmp_path / "version-pins.json"
    path.write_text("{truncated")
    stage = open_ref_pin_stage()
    try:
        with pytest.raises(RefPinStoreError, match=re.escape(str(path))):
            enforce_ref_pin("mkt", "plug", "v1", pins_dir=str(tmp_path))
        assert stage.keys() == frozenset()
    finally:
        stage.discard()
    assert path.read_text() == "{truncated"


def test_a_github_enterprise_pin_identity_ignores_owner_and_repo_case(tmp_path):
    """Re-registering a GitHub catalog with another case keeps its pins (#5169)."""
    from apm_cli.marketplace.models import MarketplaceSource
    from apm_cli.marketplace.source_identity import catalog_pin_identity

    upper = catalog_pin_identity(MarketplaceSource(name="a", url="https://acme.ghe.com/Org/Repo"))
    lower = catalog_pin_identity(MarketplaceSource(name="a", url="https://acme.ghe.com/org/repo"))
    other = catalog_pin_identity(
        MarketplaceSource(name="a", url="https://gitlab.example.com/Org/Repo")
    )
    assert upper == lower
    assert "Org/Repo" in other
    (tmp_path / "version-pins.json").write_text(
        json.dumps({"acme.ghe.com/Org/Repo:marketplace.json#tool/1.0.0": "v1"})
    )
    assert check_ref_pin("a", "tool", "v2", "1.0.0", str(tmp_path), catalog=lower) == "v1"


def test_a_hosted_catalog_query_never_reaches_the_pins_file(tmp_path):
    """A hosted catalog URL's query is hashed in its pin key, never persisted (#5169)."""
    from apm_cli.marketplace.models import MarketplaceSource
    from apm_cli.marketplace.source_identity import catalog_pin_identity

    def identity(query: str) -> str:
        url = f"https://example.com/x/marketplace.json?{query}"
        return catalog_pin_identity(MarketplaceSource(name="a", url=url, path=""))

    catalog = identity("token=s3cret")
    assert "s3cret" not in catalog
    assert catalog != identity("token=other")
    legacy = "url:example.com/x/marketplace.json?token=s3cret#tool/1.0.0"
    (tmp_path / "version-pins.json").write_text(json.dumps({legacy: "v1"}))
    assert check_ref_pin("a", "tool", "v2", "1.0.0", str(tmp_path), catalog=catalog) == "v1"

    record_ref_pin("a", "tool", "v1", "1.0.0", str(tmp_path), catalog=catalog)

    assert "s3cret" not in (tmp_path / "version-pins.json").read_text()
    assert load_ref_pins(str(tmp_path)) == {f"{catalog}#tool/1.0.0": "v1"}


def test_a_pins_lock_that_cannot_be_taken_skips_advisory_writes_and_fails_strict_ones(
    tmp_path,
):
    """An unavailable pins lock never fails a committed install; adoption refuses (#5169)."""
    from apm_cli.marketplace.version_pins import (
        RefPinWriteError,
        adopt_alias_ref_pins,
        open_ref_pin_stage,
    )

    pins_dir = str(tmp_path)
    save_ref_pins({"mkt/plug/1.0.0": "v1"}, pins_dir)
    (tmp_path / "version-pins.lock").mkdir()
    before = (tmp_path / "version-pins.json").read_bytes()
    stage = open_ref_pin_stage()
    stage.add("mkt", "other", "v2", "1.0.0", catalog="cat")

    stage.commit(pins_dir=pins_dir)
    record_ref_pin("mkt", "plug", "v3", "1.0.0", pins_dir)

    assert (tmp_path / "version-pins.json").read_bytes() == before
    with pytest.raises(RefPinWriteError):
        adopt_alias_ref_pins("mkt", "cat", pins_dir)
    assert (tmp_path / "version-pins.json").read_bytes() == before


# ---------------------------------------------------------------------------
# Same-commit pin migration and explicit accept (#5526)
# ---------------------------------------------------------------------------

_TAG_COMMIT = "5246f7b193b58a32ac8a15fc76aedf37c42b042c"
_OTHER_COMMIT = "1" * 40


def _tag_lookup(commits):
    """A ``commit_of`` for a remote whose tags select *commits*; SHAs name themselves."""
    calls = []

    def commit_of(ref):
        calls.append(ref)
        if re.fullmatch(r"[0-9a-f]{40}", ref):
            return ref
        return commits.get(ref)

    commit_of.calls = calls
    return commit_of


def _enforce_with_stage(tmp_path, ref, commit_of, *, accept_changes=False, times=1):
    from apm_cli.marketplace.version_pins import enforce_ref_pin, open_ref_pin_stage

    stage = open_ref_pin_stage(accept_changes=accept_changes)
    try:
        results = [
            enforce_ref_pin(
                "atlas", "okf", ref, "0.2.1", pins_dir=str(tmp_path), commit_of=commit_of
            )
            for _ in range(times)
        ]
        stage.commit(pins_dir=str(tmp_path))
    finally:
        stage.discard()
    return results


def test_a_tag_pin_and_the_sha_it_peels_to_migrate_the_pin_to_the_commit(tmp_path, capsys):
    """#5526: a legacy tag pin and a catalog SHA of the same commit are no ref swap."""
    save_ref_pins({"atlas/okf/0.2.1": "v0.2.1"}, pins_dir=str(tmp_path))
    lookup = _tag_lookup({"v0.2.1": _TAG_COMMIT})

    assert _enforce_with_stage(tmp_path, _TAG_COMMIT, lookup, times=2) == [None, None]

    assert load_ref_pins(pins_dir=str(tmp_path)) == {"atlas/okf/0.2.1": _TAG_COMMIT}
    out = " ".join(capsys.readouterr().out.split())
    assert out.count("same commit") == 1, out
    assert f"recorded ref 'v0.2.1' is the same commit as '{_TAG_COMMIT}'" in out
    assert lookup.calls.count("v0.2.1") == 1, "the second resolution reused the match"


def test_a_sha_pin_and_a_tag_peeling_to_it_keep_the_commit_quietly(tmp_path, capsys):
    save_ref_pins({"atlas/okf/0.2.1": _TAG_COMMIT}, pins_dir=str(tmp_path))

    _enforce_with_stage(tmp_path, "v0.2.1", _tag_lookup({"v0.2.1": _TAG_COMMIT}))

    assert load_ref_pins(pins_dir=str(tmp_path)) == {"atlas/okf/0.2.1": _TAG_COMMIT}
    assert "same commit" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "lookup",
    [
        _tag_lookup({"v0.2.1": _OTHER_COMMIT}),
        _tag_lookup({}),
        None,
    ],
    ids=["different-commit", "unknown-tag", "no-lookup"],
)
def test_a_ref_whose_commit_differs_or_is_unknown_is_still_refused(tmp_path, lookup):
    from apm_cli.marketplace.version_pins import RefPinMismatchError

    save_ref_pins({"atlas/okf/0.2.1": "v0.2.1"}, pins_dir=str(tmp_path))

    with pytest.raises(RefPinMismatchError, match="ref swap attack"):
        _enforce_with_stage(tmp_path, _TAG_COMMIT, lookup)
    assert load_ref_pins(pins_dir=str(tmp_path)) == {"atlas/okf/0.2.1": "v0.2.1"}


def test_a_commit_lookup_that_fails_refuses_fail_closed(tmp_path):
    from apm_cli.marketplace.version_pins import RefPinMismatchError

    save_ref_pins({"atlas/okf/0.2.1": "v0.2.1"}, pins_dir=str(tmp_path))

    def offline(_ref):
        raise OSError("network unreachable")

    with pytest.raises(RefPinMismatchError):
        _enforce_with_stage(tmp_path, _TAG_COMMIT, offline)
    assert load_ref_pins(pins_dir=str(tmp_path)) == {"atlas/okf/0.2.1": "v0.2.1"}


def test_an_attempt_that_accepts_ref_changes_records_a_different_commit(tmp_path):
    """``apm install --accept-ref-change`` accepts a real change for that attempt only."""
    from apm_cli.marketplace.version_pins import RefPinMismatchError

    save_ref_pins({"atlas/okf/0.2.1": "v0.2.1"}, pins_dir=str(tmp_path))
    lookup = _tag_lookup({"v0.2.1": _OTHER_COMMIT})

    assert _enforce_with_stage(tmp_path, _TAG_COMMIT, lookup, accept_changes=True) == ["v0.2.1"]
    assert load_ref_pins(pins_dir=str(tmp_path)) == {"atlas/okf/0.2.1": _TAG_COMMIT}

    with pytest.raises(RefPinMismatchError):
        _enforce_with_stage(tmp_path, "v9", lookup)
