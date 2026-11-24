"""Lock-vs-alias catalog origin checks (#3912)."""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.marketplace import catalog_origin
from apm_cli.marketplace.errors import MarketplaceOriginConflictError
from apm_cli.marketplace.models import MarketplaceSource
from apm_cli.marketplace.source_identity import same_marketplace_origin

pytestmark = pytest.mark.unit

_A = "https://git.example.com/team/catalog-a"
_B = "https://git.example.com/team/catalog-b"


def _lock(*origins: str | None, alias: str = "company") -> LockFile:
    lockfile = LockFile()
    for index, origin in enumerate(origins):
        lockfile.add_dependency(
            LockedDependency(
                repo_url=f"team/pkg-{index}",
                resolved_commit="a" * 40,
                discovered_via=alias,
                marketplace_plugin_name="plugin",
                source_url=origin,
            )
        )
    return lockfile


@pytest.fixture
def registered(monkeypatch):
    def _register(url: str) -> None:
        monkeypatch.setattr(
            catalog_origin,
            "get_marketplace_by_name",
            lambda name: MarketplaceSource(name=name, url=url),
        )

    return _register


def _register_path(monkeypatch, url: str, path: str) -> None:
    monkeypatch.setattr(
        catalog_origin,
        "get_marketplace_by_name",
        lambda name: MarketplaceSource(name=name, url=url, path=path),
    )


def test_ambiguous_association_still_rejects_a_rebound_alias(registered) -> None:
    registered(_B)
    with pytest.raises(MarketplaceOriginConflictError) as exc:
        catalog_origin.require_locked_catalog_origin("company", "plugin", _lock(_A, _A))
    assert _A in str(exc.value)
    assert _B in str(exc.value)


def test_matching_and_legacy_rows_are_accepted(registered) -> None:
    registered(f"{_A}.git")
    catalog_origin.require_locked_catalog_origin("company", "plugin", _lock(_A, None))
    catalog_origin.require_locked_catalog_origin("company", "plugin", _lock(None))


def test_hosted_catalog_query_selects_a_distinct_origin() -> None:
    hosted = "https://catalog.example.com/marketplace.json"
    assert same_marketplace_origin(f"{hosted}?tenant=a", f"{hosted}?tenant=a")
    assert not same_marketplace_origin(f"{hosted}?tenant=a", f"{hosted}?tenant=b")
    assert not same_marketplace_origin(f"{hosted}?tenant=a", hosted)


def test_recased_alias_cannot_skip_the_check(registered) -> None:
    registered(_B)
    with pytest.raises(MarketplaceOriginConflictError):
        catalog_origin.require_locked_catalog_origin("Company", "Plugin", _lock(_A))


def test_catalog_path_move_in_the_same_repo_is_a_known_limit(monkeypatch) -> None:
    """The lock records only the origin, so a path move is not detected (0.38)."""
    _register_path(monkeypatch, _A, "catalogs/other/marketplace.json")
    catalog_origin.require_locked_catalog_origin("company", "plugin", _lock(_A))


def test_conflict_remediation_quotes_the_locked_origin() -> None:
    message = str(MarketplaceOriginConflictError("company", "plugin", _A, _B))
    assert f"apm marketplace add {_A} --name company" in message
    assert "remove 'plugin' from apm.yml" in message
    unsafe = str(MarketplaceOriginConflictError("company", "plugin", "https://x/$(rm)", _B))
    assert "apm marketplace add" in unsafe
    assert "run: apm marketplace add" not in unsafe or "'https://x/$(rm)'" in unsafe


def test_conflict_advice_restores_the_lock_first_and_renders_origins_as_ascii() -> None:
    message = str(MarketplaceOriginConflictError("company", "plugin", _A, _B))
    restore = message.index("restore it from version control")
    assert restore < message.index("verify that source first, then run 'apm marketplace remove")
    escaped = str(MarketplaceOriginConflictError("company", "plugin", "https://x/\x1b[31m", "\x07"))
    assert escaped.isascii() and "\x1b" not in escaped and "\x07" not in escaped
    assert "apm marketplace add https://x" not in escaped


def test_row_level_check_refuses_a_rebound_alias_and_accepts_legacy_rows(registered) -> None:
    """``apm outdated`` checks the row it already holds through the same owner (#3959)."""
    registered(_B)
    (row,) = _lock(_A).dependencies.values()
    with pytest.raises(MarketplaceOriginConflictError) as exc:
        catalog_origin.require_locked_row_origin(row)
    assert _A in str(exc.value) and _B in str(exc.value)

    (legacy,) = _lock(None).dependencies.values()
    catalog_origin.require_locked_row_origin(legacy)


_PROJECT_CONFLICT = (
    f"marketplace 'company' now points at {_B}, but apm.lock.yaml recorded 'plugin' "
    f"from {_A}. Nothing was changed. If apm.lock.yaml was not changed on purpose, "
    "restore it from version control. Otherwise, verify that source first, then run "
    "'apm marketplace remove company --yes', then run "
    f"'apm marketplace add {_A} --name company' "
    "to keep the locked marketplace (add --ref or --path if the original "
    "registration used them). To switch marketplaces on purpose, remove 'plugin' from "
    "apm.yml, then run 'apm install' to drop the plugin locked from the old marketplace, "
    "then add it back and run 'apm install' again."
)
_USER_CONFLICT = (
    f"marketplace 'company' now points at {_B}, but ~/.apm/apm.lock.yaml recorded "
    f"'plugin' from {_A}. Nothing was changed. If ~/.apm/apm.lock.yaml changed by "
    "mistake, or to switch marketplaces on purpose, remove 'plugin' from "
    "~/.apm/apm.yml, then run 'apm install -g' to drop the plugin locked from the old "
    "marketplace, then add it back and run 'apm install -g' again. Otherwise, verify "
    "that source first, then run 'apm marketplace remove company --yes', then run "
    f"'apm marketplace add {_A} --name company' to keep "
    "the locked marketplace (add --ref or --path if the original registration used them)."
)


def test_conflict_remedy_names_the_refused_scope() -> None:
    """RA-029: a user-scope refusal names the user manifest and ``apm install -g``.

    The re-register remedy keeps no ``-g``: the marketplace registry is
    user-global. #4554: the user lock is not restored from version control.
    """
    project = MarketplaceOriginConflictError("company", "plugin", _A, _B)
    user = MarketplaceOriginConflictError("company", "plugin", _A, _B, user_scope=True)

    assert str(project) == _PROJECT_CONFLICT
    assert str(user) == _USER_CONFLICT


@pytest.mark.parametrize(
    ("argv", "expected"),
    [(["-g"], _USER_CONFLICT), ([], _PROJECT_CONFLICT)],
    ids=["global", "project"],
)
def test_origin_refusal_reads_the_scope_of_the_invoking_command(registered, argv, expected) -> None:
    """RA-029: ``apm install -g x@m`` raises below install, which does not thread scope."""
    import click
    from click.testing import CliRunner

    registered(_B)

    @click.command()
    @click.option("--global", "-g", "global_", is_flag=True)
    def install(global_):
        catalog_origin.require_locked_catalog_origin("company", "plugin", _lock(_A))

    result = CliRunner().invoke(install, argv)

    assert isinstance(result.exception, MarketplaceOriginConflictError)
    assert str(result.exception) == expected


_MKT_A = "https://git.example.invalid/acme/mkta"
_MKT_B = "https://git.example.invalid/evil/mktb"


def _marketplace_row() -> LockedDependency:
    return LockedDependency(
        repo_url="acme/mkta",
        host="git.example.invalid",
        virtual_path="plugins/x",
        is_virtual=True,
        resolved_ref="v1.0.0",
        resolved_commit="a" * 40,
        discovered_via="m",
        marketplace_plugin_name="x",
        source_url=_MKT_A,
    )


@pytest.mark.parametrize(
    ("declared", "named"),
    [
        (
            "    - git: https://git.example.invalid/acme/mkta.git\n"
            "      path: plugins/x\n"
            "      ref: v1.0.0\n",
            "remove the entry 'git: https://git.example.invalid/acme/mkta.git, "
            "path: plugins/x, ref: v1.0.0' from apm.yml",
        ),
        (
            "    - git.example.invalid/acme/mkta/plugins/x#v1.0.0\n",
            "remove 'git.example.invalid/acme/mkta/plugins/x#v1.0.0' from apm.yml",
        ),
        (
            "    - {name: x, marketplace: m}\n",
            "remove the entry 'name: x, marketplace: m' from apm.yml",
        ),
    ],
    ids=["git-path", "string", "object-form"],
)
def test_rebound_alias_remedy_names_the_manifest_entry_as_written(
    monkeypatch, tmp_path, declared, named
) -> None:
    """RA-042: the switch remedy quotes the apm.yml entry, never the bare plugin name."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text(
        "name: proj\nversion: 1.0.0\ndependencies:\n  apm:\n" + declared, encoding="utf-8"
    )
    _register_path(monkeypatch, _MKT_B, "")

    with pytest.raises(MarketplaceOriginConflictError) as exc:
        catalog_origin.require_locked_row_origin(_marketplace_row(), user_scope=False)

    message = str(exc.value)
    assert f"To switch marketplaces on purpose, {named}, then run 'apm install'" in message
    assert "remove 'x' from" not in message


def test_rebound_alias_remedy_reads_the_user_manifest_at_user_scope(monkeypatch, tmp_path) -> None:
    """RA-042 at user scope: the named entry comes from ~/.apm/apm.yml."""
    home = tmp_path / "apm-home"
    home.mkdir()
    (home / "apm.yml").write_text(
        "name: user\nversion: 1.0.0\ndependencies:\n  apm:\n"
        "    - git.example.invalid/acme/mkta/plugins/x#v1.0.0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APM_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    _register_path(monkeypatch, _MKT_B, "")

    with pytest.raises(MarketplaceOriginConflictError) as exc:
        catalog_origin.require_locked_row_origin(_marketplace_row(), user_scope=True)

    assert (
        "or to switch marketplaces on purpose, remove "
        "'git.example.invalid/acme/mkta/plugins/x#v1.0.0' from ~/.apm/apm.yml, "
        "then run 'apm install -g'"
    ) in str(exc.value)
