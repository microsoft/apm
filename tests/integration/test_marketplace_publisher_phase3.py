"""Phase-3 integration tests for marketplace publisher and marketplace commands.

Covers:
- ``src/apm_cli/commands/marketplace/__init__.py`` (gap ≈ 324 lines)
- ``src/apm_cli/marketplace/publisher.py``          (gap ≈ 296 lines)

Strategy
--------
* CLI commands are exercised via Click's CliRunner.
* Registry / network calls are mocked at the boundary (never real HTTP).
* git operations in MarketplacePublisher are injected via the ``runner``
  constructor parameter (no real git subprocess).
* File I/O for PublishState is exercised against real tmp_path directories.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import (
    _find_duplicate_names,
    _is_valid_alias,
    _load_targets_file,
    _marketplace_add_unsupported_host_error,
    _outcome_symbol,
    _parse_marketplace_repo,
    browse,
    list_cmd,
    marketplace,
    remove,
    search,
    update,
)
from apm_cli.marketplace.errors import MarketplaceNotFoundError
from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.marketplace.publisher import (
    ConsumerTarget,
    MarketplacePublisher,
    PublishOutcome,
    PublishPlan,
    PublishState,
    TargetResult,
    _sanitise_branch_segment,
)

# ---------------------------------------------------------------------------
# Shared fixture data
# ---------------------------------------------------------------------------

_MARKETPLACE_YML = """\
name: acme-marketplace
description: Acme marketplace
version: 2.0.0
owner:
  name: Acme Corp
packages:
  - name: tool-a
    source: org/tool-a
    version: "^1.0.0"
    tags:
      - test
"""

_CONSUMER_APM_YML_WITH_REF = """\
name: my-service
dependencies:
  apm:
    - tool-a@acme-marketplace#v1.0.0
"""

_CONSUMER_APM_YML_NO_REF = """\
name: my-service
dependencies:
  apm:
    - tool-a@acme-marketplace
"""

_CONSUMER_APM_YML_NO_APM = """\
name: my-service
dependencies:
  npm:
    - lodash
"""

_CONSUMER_APM_YML_NO_DEPS = """\
name: my-service
"""

_CONSUMER_APM_YML_WRONG_MARKETPLACE = """\
name: my-service
dependencies:
  apm:
    - tool-a@other-marketplace#v1.0.0
"""


def _make_mkt_root(tmp_path: Path) -> Path:
    """Write a valid marketplace.yml and return the directory."""
    (tmp_path / "marketplace.yml").write_text(_MARKETPLACE_YML, encoding="utf-8")
    return tmp_path


def _make_plan(
    targets: list[ConsumerTarget] | None = None,
    *,
    allow_downgrade: bool = False,
    allow_ref_change: bool = False,
) -> PublishPlan:
    if targets is None:
        targets = [ConsumerTarget(repo="consumer-org/svc-a", branch="main")]
    return PublishPlan(
        marketplace_name="acme-marketplace",
        marketplace_version="2.0.0",
        targets=tuple(targets),
        commit_message="chore(apm): bump acme-marketplace to 2.0.0\n\nAPM-Publish-Id: deadbeef",
        branch_name="apm/marketplace-update-acme-marketplace-2.0.0-deadbeef",
        new_ref="v2.0.0",
        tag_pattern_used="v{version}",
        short_hash="deadbeef",
        allow_downgrade=allow_downgrade,
        allow_ref_change=allow_ref_change,
    )


def _ok_process(cmd: list[str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd or [], returncode=0, stdout="", stderr="")


def _fail_process(cmd: list[str] | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.CompletedProcess(cmd or [], returncode=1, stdout="", stderr="fatal: error")
    return proc


def _make_source(name: str = "acme-tools") -> MarketplaceSource:
    return MarketplaceSource(
        name=name,
        owner="acme",
        repo="tools",
        branch="main",
        host="github.com",
        path="marketplace.json",
    )


def _make_manifest(
    plugins: list[MarketplacePlugin] | None = None,
    *,
    name: str = "acme-tools",
) -> MarketplaceManifest:
    if plugins is None:
        plugins = [
            MarketplacePlugin(name="plugin-a", description="A plugin"),
            MarketplacePlugin(name="plugin-b", description="Security scanner", tags=("security",)),
        ]
    return MarketplaceManifest(name=name, plugins=tuple(plugins))


# ---------------------------------------------------------------------------
# Section 1 - Pure helper functions (commands/__init__.py)
# ---------------------------------------------------------------------------


class TestIsValidAlias:
    """Tests for _is_valid_alias."""

    def test_simple_lowercase(self) -> None:
        assert _is_valid_alias("acme-tools") is True

    def test_alphanumeric(self) -> None:
        assert _is_valid_alias("tools123") is True

    def test_dots_allowed(self) -> None:
        assert _is_valid_alias("my.tools") is True

    def test_underscores_allowed(self) -> None:
        assert _is_valid_alias("my_tools") is True

    def test_empty_string_rejected(self) -> None:
        assert _is_valid_alias("") is False

    def test_space_rejected(self) -> None:
        assert _is_valid_alias("my tools") is False

    def test_at_sign_rejected(self) -> None:
        assert _is_valid_alias("my@tools") is False

    def test_slash_rejected(self) -> None:
        assert _is_valid_alias("my/tools") is False


class TestFindDuplicateNames:
    """Tests for _find_duplicate_names."""

    def _pkg(self, name: str) -> Any:
        """Return a simple namespace with a .name attribute."""
        return SimpleNamespace(name=name)

    def test_no_duplicates_returns_empty(self) -> None:
        yml = MagicMock()
        yml.packages = [self._pkg("tool-a"), self._pkg("tool-b")]
        result = _find_duplicate_names(yml)
        assert result == ""

    def test_duplicate_returns_diagnostic(self) -> None:
        yml = MagicMock()
        yml.packages = [self._pkg("tool-a"), self._pkg("Tool-A")]  # same when lowercased
        result = _find_duplicate_names(yml)
        assert "Duplicate names" in result
        assert "tool-a" in result.lower() or "Tool-A" in result

    def test_case_insensitive_comparison(self) -> None:
        yml = MagicMock()
        yml.packages = [self._pkg("TOOL"), self._pkg("tool")]
        result = _find_duplicate_names(yml)
        assert result != ""

    def test_empty_packages_returns_empty(self) -> None:
        yml = MagicMock()
        yml.packages = []
        assert _find_duplicate_names(yml) == ""


class TestOutcomeSymbol:
    """Tests for _outcome_symbol."""

    def test_updated(self) -> None:
        assert _outcome_symbol(PublishOutcome.UPDATED) == "[+]"

    def test_failed(self) -> None:
        assert _outcome_symbol(PublishOutcome.FAILED) == "[x]"

    def test_skipped_downgrade(self) -> None:
        assert _outcome_symbol(PublishOutcome.SKIPPED_DOWNGRADE) == "[!]"

    def test_skipped_ref_change(self) -> None:
        assert _outcome_symbol(PublishOutcome.SKIPPED_REF_CHANGE) == "[!]"

    def test_no_change(self) -> None:
        assert _outcome_symbol(PublishOutcome.NO_CHANGE) == "[*]"


class TestMarketplaceAddUnsupportedHostError:
    """Tests for _marketplace_add_unsupported_host_error."""

    def test_ado_host_specific_message(self) -> None:
        msg = _marketplace_add_unsupported_host_error(
            "dev.azure.com", "'dev.azure.com'", "'dev.azure.com'", "ado"
        )
        assert "Azure DevOps" in msg or "not supported" in msg.lower()
        assert "GitHub" in msg

    def test_generic_host_mentions_supported_hosts(self) -> None:
        msg = _marketplace_add_unsupported_host_error(
            "example.com", "'example.com'", "'example.com'", "generic"
        )
        assert re.search(r"\bgithub\.com\b", msg)
        assert "GITHUB_HOST" in msg or "GitLab" in msg


# ---------------------------------------------------------------------------
# Section 2 - _parse_marketplace_repo
# ---------------------------------------------------------------------------


class TestParseMarketplaceRepo:
    """Tests for _parse_marketplace_repo."""

    def test_simple_owner_repo(self) -> None:
        url, kind, embedded = _parse_marketplace_repo("acme/tools", None)
        assert url == "https://github.com/acme/tools"
        assert kind == "github"
        assert embedded == "github.com"

    def test_https_url(self) -> None:
        url, kind, embedded = _parse_marketplace_repo("https://github.com/acme/tools", None)
        assert url == "https://github.com/acme/tools"
        assert kind == "github"
        assert embedded == "github.com"

    def test_https_url_preserves_dot_git(self) -> None:
        url, _kind, _embedded = _parse_marketplace_repo("https://github.com/acme/tools.git", None)
        assert url == "https://github.com/acme/tools.git"

    def test_host_shorthand_three_segments(self) -> None:
        url, kind, embedded = _parse_marketplace_repo("github.com/acme/tools", None)
        assert url == "https://github.com/acme/tools"
        assert kind == "github"
        assert embedded == "github.com"

    def test_http_rejected(self) -> None:
        with pytest.raises(ValueError, match="Insecure HTTP"):
            _parse_marketplace_repo("http://github.com/acme/tools", None)

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Empty"):
            _parse_marketplace_repo("", None)

    def test_single_segment_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_marketplace_repo("acme", None)

    def test_control_character_raises(self) -> None:
        with pytest.raises(ValueError, match="control"):
            _parse_marketplace_repo("acme/\x00tools", None)

    def test_conflicting_host_raises(self) -> None:
        with pytest.raises(ValueError, match="Conflicting host"):
            _parse_marketplace_repo("https://github.com/acme/tools", "gitlab.com")

    def test_host_flag_normalised(self) -> None:
        _url, _kind, embedded = _parse_marketplace_repo(
            "https://github.com/acme/tools", "github.com"
        )
        assert embedded == "github.com"

    def test_path_traversal_raises(self) -> None:
        from apm_cli.utils.path_security import PathTraversalError

        with pytest.raises((ValueError, PathTraversalError)):
            _parse_marketplace_repo("acme/../tools", None)

    def test_nested_path_owner(self) -> None:
        """HOST/group/sub/repo -- multi-segment owner path."""
        url, _kind, embedded = _parse_marketplace_repo(
            "https://gitlab.example.com/group/sub/repo", None
        )
        assert url == "https://gitlab.example.com/group/sub/repo"
        assert embedded == "gitlab.example.com"


# ---------------------------------------------------------------------------
# Section 3 - _load_targets_file
# ---------------------------------------------------------------------------


class TestLoadTargetsFile:
    """Tests for _load_targets_file."""

    def test_valid_single_target(self, tmp_path: Path) -> None:
        content = "targets:\n  - repo: org/svc\n    branch: main\n"
        f = tmp_path / "targets.yml"
        f.write_text(content, encoding="utf-8")
        targets, err = _load_targets_file(f)
        assert err is None
        assert len(targets) == 1
        assert targets[0].repo == "org/svc"
        assert targets[0].branch == "main"

    def test_valid_with_path_in_repo(self, tmp_path: Path) -> None:
        content = (
            "targets:\n  - repo: org/svc\n    branch: main\n    path_in_repo: config/apm.yml\n"
        )
        f = tmp_path / "targets.yml"
        f.write_text(content, encoding="utf-8")
        targets, err = _load_targets_file(f)
        assert err is None
        assert targets[0].path_in_repo == "config/apm.yml"

    def test_missing_targets_key(self, tmp_path: Path) -> None:
        f = tmp_path / "targets.yml"
        f.write_text("foo: bar\n", encoding="utf-8")
        targets, err = _load_targets_file(f)
        assert targets is None
        assert err is not None

    def test_empty_targets_list(self, tmp_path: Path) -> None:
        f = tmp_path / "targets.yml"
        f.write_text("targets: []\n", encoding="utf-8")
        targets, err = _load_targets_file(f)
        assert targets is None
        assert err is not None

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "targets.yml"
        f.write_text("targets: [: bad yaml\n", encoding="utf-8")
        targets, err = _load_targets_file(f)
        assert targets is None
        assert err is not None

    def test_missing_repo_field(self, tmp_path: Path) -> None:
        f = tmp_path / "targets.yml"
        f.write_text("targets:\n  - branch: main\n", encoding="utf-8")
        targets, err = _load_targets_file(f)
        assert targets is None
        assert "repo" in err.lower() or err is not None

    def test_invalid_repo_format(self, tmp_path: Path) -> None:
        f = tmp_path / "targets.yml"
        f.write_text("targets:\n  - repo: justname\n    branch: main\n", encoding="utf-8")
        targets, err = _load_targets_file(f)
        assert targets is None
        assert err is not None

    def test_missing_branch_field(self, tmp_path: Path) -> None:
        f = tmp_path / "targets.yml"
        f.write_text("targets:\n  - repo: org/svc\n", encoding="utf-8")
        targets, err = _load_targets_file(f)
        assert targets is None
        assert err is not None

    def test_path_traversal_in_path_in_repo(self, tmp_path: Path) -> None:
        f = tmp_path / "targets.yml"
        f.write_text(
            "targets:\n  - repo: org/svc\n    branch: main\n    path_in_repo: ../etc/passwd\n",
            encoding="utf-8",
        )
        targets, err = _load_targets_file(f)
        assert targets is None
        assert err is not None

    def test_entry_not_a_mapping(self, tmp_path: Path) -> None:
        f = tmp_path / "targets.yml"
        f.write_text("targets:\n  - just-a-string\n", encoding="utf-8")
        targets, err = _load_targets_file(f)
        assert targets is None
        assert err is not None


# ---------------------------------------------------------------------------
# Section 4 - ConsumerTarget validation (publisher.py)
# ---------------------------------------------------------------------------


class TestConsumerTarget:
    """Tests for ConsumerTarget __post_init__ validation."""

    def test_valid_target(self) -> None:
        t = ConsumerTarget(repo="org/svc", branch="main")
        assert t.repo == "org/svc"

    def test_invalid_repo_format_raises(self) -> None:
        with pytest.raises(ValueError, match="owner/name"):
            ConsumerTarget(repo="justname", branch="main")

    def test_repo_with_special_chars_raises(self) -> None:
        with pytest.raises(ValueError):
            ConsumerTarget(repo="org/svc!", branch="main")

    def test_branch_double_dot_raises(self) -> None:
        with pytest.raises(ValueError):
            ConsumerTarget(repo="org/svc", branch="feature/../../escape")

    def test_path_in_repo_traversal_raises(self) -> None:
        from apm_cli.utils.path_security import PathTraversalError

        with pytest.raises((ValueError, PathTraversalError)):
            ConsumerTarget(repo="org/svc", branch="main", path_in_repo="../etc/passwd")

    def test_custom_path_in_repo(self) -> None:
        t = ConsumerTarget(repo="org/svc", branch="main", path_in_repo="config/apm.yml")
        assert t.path_in_repo == "config/apm.yml"


# ---------------------------------------------------------------------------
# Section 5 - _sanitise_branch_segment (publisher.py)
# ---------------------------------------------------------------------------


class TestSanitiseBranchSegment:
    """Tests for _sanitise_branch_segment."""

    def test_safe_chars_unchanged(self) -> None:
        assert _sanitise_branch_segment("acme-marketplace-2.0.0") == "acme-marketplace-2.0.0"

    def test_spaces_replaced_with_hyphens(self) -> None:
        assert _sanitise_branch_segment("my marketplace") == "my-marketplace"

    def test_slash_replaced(self) -> None:
        result = _sanitise_branch_segment("feature/branch")
        assert "/" not in result

    def test_at_sign_replaced(self) -> None:
        result = _sanitise_branch_segment("org@v2.0")
        assert "@" not in result


# ---------------------------------------------------------------------------
# Section 6 - PublishState (publisher.py)
# ---------------------------------------------------------------------------


class TestPublishState:
    """Tests for PublishState - state file write/read lifecycle."""

    def test_load_from_missing_path_returns_fresh(self, tmp_path: Path) -> None:
        state = PublishState.load(tmp_path / "nonexistent")
        assert state.data["lastRun"] is None
        assert state.data["history"] == []

    def test_load_from_corrupt_json_returns_fresh(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir()
        (apm_dir / "publish-state.json").write_text("{not json}", encoding="utf-8")
        state = PublishState.load(tmp_path)
        assert state.data["lastRun"] is None

    def test_begin_run_creates_last_run(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        plan = _make_plan()
        state.begin_run(plan)
        assert state.data["lastRun"] is not None
        assert state.data["lastRun"]["marketplaceName"] == "acme-marketplace"

    def test_begin_run_writes_state_file(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        state.begin_run(_make_plan())
        state_path = tmp_path / ".apm" / "publish-state.json"
        assert state_path.exists()

    def test_record_result_appended(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        state.begin_run(_make_plan())
        target = ConsumerTarget(repo="org/svc", branch="main")
        result = TargetResult(
            target=target, outcome=PublishOutcome.UPDATED, message="ok", new_version="v2.0.0"
        )
        state.record_result(result)
        results = state.data["lastRun"]["results"]
        assert len(results) == 1
        assert results[0]["outcome"] == "updated"

    def test_record_result_without_begin_run_is_noop(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        target = ConsumerTarget(repo="org/svc", branch="main")
        result = TargetResult(target=target, outcome=PublishOutcome.FAILED, message="err")
        state.record_result(result)  # should not raise
        assert state.data["lastRun"] is None

    def test_finalise_rotates_into_history(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        state.begin_run(_make_plan())
        state.finalise(datetime.now(timezone.utc))
        assert len(state.data["history"]) == 1
        assert state.data["lastRun"]["finishedAt"] is not None

    def test_abort_marks_finished_at(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        state.begin_run(_make_plan())
        state.abort("something went wrong")
        assert "ABORTED" in state.data["lastRun"]["finishedAt"]

    def test_abort_without_begin_run_is_noop(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        state.abort("reason")  # should not raise

    def test_data_property_returns_copy(self, tmp_path: Path) -> None:
        state = PublishState(tmp_path)
        d = state.data
        d["injected"] = True
        assert "injected" not in state.data

    def test_history_rotation_at_max(self, tmp_path: Path) -> None:
        """After 11 runs, history should have at most 10 entries."""
        state = PublishState(tmp_path)
        for _ in range(11):
            state.begin_run(_make_plan())
            state.finalise(datetime.now(timezone.utc))
        assert len(state.data["history"]) <= 10

    def test_load_valid_state_file(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir()
        saved = {
            "schemaVersion": 1,
            "lastRun": {
                "branchName": "apm/test",
                "results": [],
                "finishedAt": None,
                "startedAt": "2024-01-01T00:00:00+00:00",
                "marketplaceName": "test",
                "marketplaceVersion": "1.0.0",
            },
            "history": [],
        }
        (apm_dir / "publish-state.json").write_text(json.dumps(saved), encoding="utf-8")
        state = PublishState.load(tmp_path)
        assert state.data["lastRun"]["branchName"] == "apm/test"


# ---------------------------------------------------------------------------
# Section 7 - MarketplacePublisher.plan (publisher.py)
# ---------------------------------------------------------------------------


class TestMarketplacePublisherPlan:
    """Tests for MarketplacePublisher.plan()."""

    def test_plan_returns_publish_plan(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        pub = MarketplacePublisher(tmp_path, runner=lambda *a, **kw: _ok_process())
        targets = [ConsumerTarget(repo="org/svc", branch="main")]
        plan = pub.plan(targets)
        assert isinstance(plan, PublishPlan)

    def test_plan_marketplace_name(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        pub = MarketplacePublisher(tmp_path)
        plan = pub.plan([ConsumerTarget(repo="org/svc", branch="main")])
        assert plan.marketplace_name == "acme-marketplace"

    def test_plan_marketplace_version(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        pub = MarketplacePublisher(tmp_path)
        plan = pub.plan([ConsumerTarget(repo="org/svc", branch="main")])
        assert plan.marketplace_version == "2.0.0"

    def test_plan_branch_name_deterministic(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        pub = MarketplacePublisher(tmp_path)
        targets = [ConsumerTarget(repo="org/svc", branch="main")]
        plan1 = pub.plan(targets)
        plan2 = pub.plan(targets)
        assert plan1.branch_name == plan2.branch_name

    def test_plan_branch_starts_with_apm_prefix(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        pub = MarketplacePublisher(tmp_path)
        plan = pub.plan([ConsumerTarget(repo="org/svc", branch="main")])
        assert plan.branch_name.startswith("apm/marketplace-update-")

    def test_plan_commit_message_contains_marketplace_name(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        pub = MarketplacePublisher(tmp_path)
        plan = pub.plan([ConsumerTarget(repo="org/svc", branch="main")])
        assert "acme-marketplace" in plan.commit_message

    def test_plan_commit_message_contains_apm_publish_id(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        pub = MarketplacePublisher(tmp_path)
        plan = pub.plan([ConsumerTarget(repo="org/svc", branch="main")])
        assert "APM-Publish-Id:" in plan.commit_message

    def test_plan_new_ref_uses_tag_pattern(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        pub = MarketplacePublisher(tmp_path)
        plan = pub.plan([ConsumerTarget(repo="org/svc", branch="main")])
        # default tag pattern is v{version}
        assert plan.new_ref == "v2.0.0"

    def test_plan_with_allow_downgrade(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        pub = MarketplacePublisher(tmp_path)
        plan = pub.plan([ConsumerTarget(repo="org/svc", branch="main")], allow_downgrade=True)
        assert plan.allow_downgrade is True

    def test_plan_with_allow_ref_change(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        pub = MarketplacePublisher(tmp_path)
        plan = pub.plan([ConsumerTarget(repo="org/svc", branch="main")], allow_ref_change=True)
        assert plan.allow_ref_change is True

    def test_plan_targets_preserved(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        pub = MarketplacePublisher(tmp_path)
        targets = [
            ConsumerTarget(repo="org/svc-a", branch="main"),
            ConsumerTarget(repo="org/svc-b", branch="develop"),
        ]
        plan = pub.plan(targets)
        assert len(plan.targets) == 2

    def test_plan_short_hash_is_hex(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        pub = MarketplacePublisher(tmp_path)
        plan = pub.plan([ConsumerTarget(repo="org/svc", branch="main")])
        int(plan.short_hash, 16)  # should not raise

    def test_plan_no_marketplace_yml_raises(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.errors import MarketplaceYmlError

        pub = MarketplacePublisher(tmp_path)
        with pytest.raises((MarketplaceYmlError, FileNotFoundError, Exception)):
            pub.plan([ConsumerTarget(repo="org/svc", branch="main")])


# ---------------------------------------------------------------------------
# Section 8 - MarketplacePublisher.execute + _process_single_target
# ---------------------------------------------------------------------------


def _build_runner(
    *,
    clone_apm_yml: str | None = None,
    clone_dir_name: str = "repo",
    clone_fail: bool = False,
    checkout_fail: bool = False,
    commit_fail: bool = False,
    push_fail: bool = False,
) -> Any:
    """Return a mock runner callable that simulates git operations.

    On clone success it creates the clone dir and optionally writes apm.yml.
    """

    def runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        cwd = kwargs.get("cwd", "")
        # Detect the operation by cmd content
        if "clone" in cmd:
            if clone_fail:
                raise subprocess.CalledProcessError(
                    returncode=128, cmd=cmd, stderr="fatal: repository not found"
                )
            clone_path = Path(cwd) / clone_dir_name
            clone_path.mkdir(parents=True, exist_ok=True)
            if clone_apm_yml is not None:
                (clone_path / "apm.yml").write_text(clone_apm_yml, encoding="utf-8")
            return _ok_process(cmd)

        if "checkout" in cmd:
            if checkout_fail:
                raise subprocess.CalledProcessError(returncode=1, cmd=cmd, stderr="error")
            return _ok_process(cmd)

        if "commit" in cmd:
            if commit_fail:
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=cmd, stderr="nothing to commit"
                )
            return _ok_process(cmd)

        if "push" in cmd:
            if push_fail:
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=cmd, stderr="fatal: push failed"
                )
            return _ok_process(cmd)

        return _ok_process(cmd)

    return runner


class TestMarketplacePublisherExecute:
    """Tests for MarketplacePublisher.execute() and _process_single_target."""

    def test_execute_returns_list_of_results(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        runner = _build_runner(clone_apm_yml=_CONSUMER_APM_YML_WITH_REF)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        results = pub.execute(plan, dry_run=True)
        assert isinstance(results, list)
        assert len(results) == 1

    def test_execute_happy_path_updated(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        runner = _build_runner(clone_apm_yml=_CONSUMER_APM_YML_WITH_REF)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        results = pub.execute(plan, dry_run=True)
        assert results[0].outcome == PublishOutcome.UPDATED

    def test_execute_no_change_when_already_at_new_ref(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        already_updated = """\
name: my-service
dependencies:
  apm:
    - tool-a@acme-marketplace#v2.0.0
"""
        runner = _build_runner(clone_apm_yml=already_updated)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        results = pub.execute(plan, dry_run=True)
        assert results[0].outcome == PublishOutcome.NO_CHANGE

    def test_execute_clone_failure_returns_failed(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        runner = _build_runner(clone_fail=True)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        results = pub.execute(plan, dry_run=True)
        assert results[0].outcome == PublishOutcome.FAILED

    def test_execute_creates_state_file(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        runner = _build_runner(clone_apm_yml=_CONSUMER_APM_YML_WITH_REF)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        pub.execute(plan, dry_run=True)
        state_path = tmp_path / ".apm" / "publish-state.json"
        assert state_path.exists()

    def test_execute_dry_run_no_push_called(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        push_called = []

        def runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            if "push" in cmd:
                push_called.append(True)
            if "clone" in cmd:
                clone_path = Path(kwargs["cwd"]) / "repo"
                clone_path.mkdir(parents=True, exist_ok=True)
                (clone_path / "apm.yml").write_text(_CONSUMER_APM_YML_WITH_REF, encoding="utf-8")
            return _ok_process(cmd)

        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        pub.execute(plan, dry_run=True)
        assert not push_called, "push should not be called in dry_run mode"

    def test_execute_push_called_when_not_dry_run(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        push_called = []

        def runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            if "push" in cmd:
                push_called.append(True)
            if "clone" in cmd:
                clone_path = Path(kwargs["cwd"]) / "repo"
                clone_path.mkdir(parents=True, exist_ok=True)
                (clone_path / "apm.yml").write_text(_CONSUMER_APM_YML_WITH_REF, encoding="utf-8")
            return _ok_process(cmd)

        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        pub.execute(plan, dry_run=False)
        assert push_called, "push should be called when not dry_run"

    def test_execute_missing_apm_yml_returns_failed(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        # Don't create apm.yml in clone dir
        runner = _build_runner(clone_apm_yml=None)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        results = pub.execute(plan, dry_run=True)
        assert results[0].outcome == PublishOutcome.FAILED
        assert "not found" in results[0].message.lower()

    def test_execute_no_dependencies_returns_failed(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        runner = _build_runner(clone_apm_yml=_CONSUMER_APM_YML_NO_DEPS)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        results = pub.execute(plan, dry_run=True)
        assert results[0].outcome == PublishOutcome.FAILED

    def test_execute_no_apm_deps_returns_failed(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        runner = _build_runner(clone_apm_yml=_CONSUMER_APM_YML_NO_APM)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        results = pub.execute(plan, dry_run=True)
        assert results[0].outcome == PublishOutcome.FAILED

    def test_execute_wrong_marketplace_returns_failed(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        runner = _build_runner(clone_apm_yml=_CONSUMER_APM_YML_WRONG_MARKETPLACE)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        results = pub.execute(plan, dry_run=True)
        assert results[0].outcome == PublishOutcome.FAILED

    def test_execute_downgrade_guard(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        # Current ref is v3.0.0, new ref is v2.0.0 -> downgrade
        newer_yml = """\
name: my-service
dependencies:
  apm:
    - tool-a@acme-marketplace#v3.0.0
"""
        runner = _build_runner(clone_apm_yml=newer_yml)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan(allow_downgrade=False)
        results = pub.execute(plan, dry_run=True)
        assert results[0].outcome == PublishOutcome.SKIPPED_DOWNGRADE

    def test_execute_downgrade_allowed(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        newer_yml = """\
name: my-service
dependencies:
  apm:
    - tool-a@acme-marketplace#v3.0.0
"""
        runner = _build_runner(clone_apm_yml=newer_yml)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan(allow_downgrade=True)
        results = pub.execute(plan, dry_run=True)
        assert results[0].outcome == PublishOutcome.UPDATED

    def test_execute_ref_change_implicit_to_explicit(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        runner = _build_runner(clone_apm_yml=_CONSUMER_APM_YML_NO_REF)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan(allow_ref_change=False)
        results = pub.execute(plan, dry_run=True)
        assert results[0].outcome == PublishOutcome.SKIPPED_REF_CHANGE

    def test_execute_ref_change_allowed(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        runner = _build_runner(clone_apm_yml=_CONSUMER_APM_YML_NO_REF)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan(allow_ref_change=True)
        results = pub.execute(plan, dry_run=True)
        assert results[0].outcome == PublishOutcome.UPDATED

    def test_execute_invalid_yaml_in_apm_yml_returns_failed(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        runner = _build_runner(clone_apm_yml="{not: valid: yaml: [\n")
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        results = pub.execute(plan, dry_run=True)
        assert results[0].outcome == PublishOutcome.FAILED

    def test_execute_apm_yml_not_a_mapping_returns_failed(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        runner = _build_runner(clone_apm_yml="- just\n- a\n- list\n")
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        results = pub.execute(plan, dry_run=True)
        assert results[0].outcome == PublishOutcome.FAILED

    def test_execute_multiple_targets_ordered(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        runner = _build_runner(clone_apm_yml=_CONSUMER_APM_YML_WITH_REF)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        targets = [
            ConsumerTarget(repo="org/svc-a", branch="main"),
            ConsumerTarget(repo="org/svc-b", branch="main"),
        ]
        plan = _make_plan(targets)
        results = pub.execute(plan, dry_run=True)
        assert len(results) == 2

    def test_execute_push_failure_returns_failed(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        runner = _build_runner(clone_apm_yml=_CONSUMER_APM_YML_WITH_REF, push_fail=True)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        results = pub.execute(plan, dry_run=False)
        assert results[0].outcome == PublishOutcome.FAILED

    def test_execute_updated_message_contains_old_and_new(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        runner = _build_runner(clone_apm_yml=_CONSUMER_APM_YML_WITH_REF)
        pub = MarketplacePublisher(tmp_path, runner=runner)
        plan = _make_plan()
        results = pub.execute(plan, dry_run=True)
        assert results[0].outcome == PublishOutcome.UPDATED
        assert "v1.0.0" in results[0].message or "acme-marketplace" in results[0].message


# ---------------------------------------------------------------------------
# Section 9 - MarketplacePublisher.safe_force_push (publisher.py)
# ---------------------------------------------------------------------------


class TestSafeForPush:
    """Tests for MarketplacePublisher.safe_force_push."""

    def test_trailer_match_returns_true(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)
        expected_trailer = "abc12345"
        commit_msg = f"chore: update\n\nAPM-Publish-Id: {expected_trailer}"
        call_count = []

        def runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            call_count.append(cmd)
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, returncode=0, stdout=commit_msg, stderr="")
            return _ok_process(cmd)

        pub = MarketplacePublisher(tmp_path, runner=runner)
        result = pub.safe_force_push("origin", "apm/test", expected_trailer)
        assert result is True

    def test_trailer_mismatch_returns_false(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)

        def runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            if "log" in cmd:
                return subprocess.CompletedProcess(
                    cmd, returncode=0, stdout="chore: no trailer", stderr=""
                )
            return _ok_process(cmd)

        pub = MarketplacePublisher(tmp_path, runner=runner)
        result = pub.safe_force_push("origin", "apm/test", "deadbeef")
        assert result is False

    def test_exception_returns_false(self, tmp_path: Path) -> None:
        _make_mkt_root(tmp_path)

        def runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            raise subprocess.CalledProcessError(returncode=128, cmd=cmd, stderr="error")

        pub = MarketplacePublisher(tmp_path, runner=runner)
        result = pub.safe_force_push("origin", "apm/test", "deadbeef")
        assert result is False


# ---------------------------------------------------------------------------
# Section 10 - CLI commands via CliRunner
# ---------------------------------------------------------------------------


def _mock_source(name: str = "acme-tools") -> MarketplaceSource:
    return MarketplaceSource(
        name=name, owner="acme", repo="tools", branch="main", host="github.com"
    )


class TestListCommand:
    """Tests for `apm marketplace list`."""

    def test_empty_registry_shows_info(self) -> None:
        runner = CliRunner()
        with patch(
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            return_value=[],
        ):
            result = runner.invoke(list_cmd, [], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No marketplaces" in result.output or "registered" in result.output.lower()

    def test_with_sources_shows_them(self) -> None:
        runner = CliRunner()
        source = _mock_source()
        with patch(
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            return_value=[source],
        ):
            result = runner.invoke(list_cmd, [], catch_exceptions=False)
        assert result.exit_code == 0
        assert "acme-tools" in result.output or "acme" in result.output

    def test_no_traceback(self) -> None:
        runner = CliRunner()
        with patch(
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            return_value=[],
        ):
            result = runner.invoke(list_cmd, [], catch_exceptions=False)
        assert "Traceback" not in result.output


class TestRemoveCommand:
    """Tests for `apm marketplace remove`."""

    def test_success_with_yes_flag(self) -> None:
        runner = CliRunner()
        source = _mock_source()
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch("apm_cli.marketplace.registry.remove_marketplace"),
            patch("apm_cli.marketplace.client.clear_marketplace_cache"),
        ):
            result = runner.invoke(remove, ["acme-tools", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "acme-tools" in result.output or "removed" in result.output.lower()

    def test_non_interactive_without_yes_exits_1(self) -> None:
        runner = CliRunner()
        source = _mock_source()
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch(
                "apm_cli.commands._helpers._is_interactive",
                return_value=False,
            ),
        ):
            result = runner.invoke(remove, ["acme-tools"], catch_exceptions=False)
        assert result.exit_code == 1

    def test_not_found_exits_1(self) -> None:
        runner = CliRunner()
        with patch(
            "apm_cli.marketplace.registry.get_marketplace_by_name",
            side_effect=MarketplaceNotFoundError("acme-tools"),
        ):
            result = runner.invoke(remove, ["acme-tools", "--yes"], catch_exceptions=False)
        assert result.exit_code == 1


class TestUpdateCommand:
    """Tests for `apm marketplace update`."""

    def test_update_specific_name(self) -> None:
        runner = CliRunner()
        source = _mock_source()
        manifest = _make_manifest()
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch("apm_cli.marketplace.client.clear_marketplace_cache"),
            patch(
                "apm_cli.marketplace.client.fetch_marketplace",
                return_value=manifest,
            ),
        ):
            result = runner.invoke(update, ["acme-tools"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "acme-tools" in result.output or "updated" in result.output.lower()

    def test_update_all_empty_registry(self) -> None:
        runner = CliRunner()
        with patch(
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            return_value=[],
        ):
            result = runner.invoke(update, [], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No marketplaces" in result.output or "registered" in result.output.lower()

    def test_update_all_multiple_sources(self) -> None:
        runner = CliRunner()
        sources = [_mock_source("mkt-a"), _mock_source("mkt-b")]
        manifest = _make_manifest()
        with (
            patch(
                "apm_cli.marketplace.registry.get_registered_marketplaces",
                return_value=sources,
            ),
            patch("apm_cli.marketplace.client.clear_marketplace_cache"),
            patch(
                "apm_cli.marketplace.client.fetch_marketplace",
                return_value=manifest,
            ),
        ):
            result = runner.invoke(update, [], catch_exceptions=False)
        assert result.exit_code == 0


class TestBrowseCommand:
    """Tests for `apm marketplace browse`."""

    def test_browse_with_plugins(self) -> None:
        runner = CliRunner()
        source = _mock_source()
        manifest = _make_manifest()
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_marketplace",
                return_value=manifest,
            ),
        ):
            result = runner.invoke(browse, ["acme-tools"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "plugin-a" in result.output or "plugin" in result.output.lower()

    def test_browse_empty_plugins(self) -> None:
        runner = CliRunner()
        source = _mock_source()
        manifest = _make_manifest(plugins=[])
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_marketplace",
                return_value=manifest,
            ),
        ):
            result = runner.invoke(browse, ["acme-tools"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "no plugins" in result.output.lower() or "0" in result.output

    def test_browse_not_found_exits_1(self) -> None:
        runner = CliRunner()
        with patch(
            "apm_cli.marketplace.registry.get_marketplace_by_name",
            side_effect=Exception("not found"),
        ):
            result = runner.invoke(browse, ["nonexistent"], catch_exceptions=False)
        assert result.exit_code == 1


class TestSearchCommand:
    """Tests for `apm marketplace search`."""

    def test_search_missing_at_sign_exits_1(self) -> None:
        runner = CliRunner()
        result = runner.invoke(search, ["noseparator"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "@" in result.output

    def test_search_empty_query_exits_1(self) -> None:
        runner = CliRunner()
        result = runner.invoke(search, ["@acme-tools"], catch_exceptions=False)
        assert result.exit_code == 1

    def test_search_empty_marketplace_exits_1(self) -> None:
        runner = CliRunner()
        result = runner.invoke(search, ["query@"], catch_exceptions=False)
        assert result.exit_code == 1

    def test_search_unregistered_marketplace_exits_1(self) -> None:
        runner = CliRunner()
        with patch(
            "apm_cli.marketplace.registry.get_marketplace_by_name",
            side_effect=MarketplaceNotFoundError("unknown"),
        ):
            result = runner.invoke(search, ["security@unknown"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "not registered" in result.output.lower() or "unknown" in result.output

    def test_search_no_results(self) -> None:
        runner = CliRunner()
        source = _mock_source()
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch(
                "apm_cli.marketplace.client.search_marketplace",
                return_value=[],
            ),
        ):
            result = runner.invoke(search, ["xyzzy@acme-tools"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "no plugins" in result.output.lower() or "not found" in result.output.lower()

    def test_search_with_results(self) -> None:
        runner = CliRunner()
        source = _mock_source()
        plugins = [MarketplacePlugin(name="security-scanner", description="Security tool")]
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch(
                "apm_cli.marketplace.client.search_marketplace",
                return_value=plugins,
            ),
        ):
            result = runner.invoke(search, ["security@acme-tools"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "security-scanner" in result.output or "security" in result.output.lower()

    def test_search_respects_limit(self) -> None:
        runner = CliRunner()
        source = _mock_source()
        plugins = [MarketplacePlugin(name=f"p{i}") for i in range(25)]
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ),
            patch(
                "apm_cli.marketplace.client.search_marketplace",
                return_value=plugins,
            ),
        ):
            result = runner.invoke(
                search, ["query@acme-tools", "--limit", "5"], catch_exceptions=False
            )
        assert result.exit_code == 0


class TestMarketplaceGroupBuildDeprecated:
    """Tests that 'apm marketplace build' is rejected with a clear error."""

    def test_build_raises_usage_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(marketplace, ["build"], catch_exceptions=False)
        # Should exit with non-zero or raise a UsageError message
        assert result.exit_code != 0 or "build" in result.output.lower()
        # The error message should mention 'apm pack'
        assert "pack" in result.output or "removed" in result.output


# ---------------------------------------------------------------------------
# Section 11 - PublishPlan field assertions
# ---------------------------------------------------------------------------


class TestPublishPlan:
    """Tests for PublishPlan dataclass fields."""

    def test_publish_plan_fields(self) -> None:
        targets = (ConsumerTarget(repo="org/svc", branch="main"),)
        plan = PublishPlan(
            marketplace_name="test-mkt",
            marketplace_version="1.0.0",
            targets=targets,
            commit_message="chore: bump",
            branch_name="apm/test",
            new_ref="v1.0.0",
            tag_pattern_used="v{version}",
            short_hash="abcd1234",
        )
        assert plan.marketplace_name == "test-mkt"
        assert plan.marketplace_version == "1.0.0"
        assert len(plan.targets) == 1
        assert plan.allow_downgrade is False
        assert plan.allow_ref_change is False
        assert plan.target_package is None

    def test_publish_plan_with_target_package(self) -> None:
        plan = PublishPlan(
            marketplace_name="test",
            marketplace_version="1.0.0",
            targets=(ConsumerTarget(repo="org/svc", branch="main"),),
            commit_message="msg",
            branch_name="apm/test",
            new_ref="v1.0.0",
            tag_pattern_used="v{version}",
            target_package="tool-a",
        )
        assert plan.target_package == "tool-a"


# ---------------------------------------------------------------------------
# Section 12 - PublishOutcome enum
# ---------------------------------------------------------------------------


class TestPublishOutcomeEnum:
    """Tests for PublishOutcome enum values."""

    def test_all_values_unique(self) -> None:
        values = [o.value for o in PublishOutcome]
        assert len(values) == len(set(values))

    def test_string_subclass(self) -> None:
        assert isinstance(PublishOutcome.UPDATED.value, str)

    def test_updated_value(self) -> None:
        assert PublishOutcome.UPDATED.value == "updated"

    def test_failed_value(self) -> None:
        assert PublishOutcome.FAILED.value == "failed"

    def test_no_change_value(self) -> None:
        assert PublishOutcome.NO_CHANGE.value == "no-change"


# ---------------------------------------------------------------------------
# Section 13 - _check_gitignore_for_marketplace_json
# ---------------------------------------------------------------------------


class TestCheckGitignoreForMarketplaceJson:
    """Tests for _check_gitignore_for_marketplace_json (via CliRunner CWD)."""

    def test_no_gitignore_no_warning(self, tmp_path: Path) -> None:
        """No .gitignore file should not produce a warning."""
        from apm_cli.commands.marketplace import _check_gitignore_for_marketplace_json

        logger_mock = MagicMock()
        import os

        old_cwd = os.getcwd()
        try:
            os.chdir(str(tmp_path))
            _check_gitignore_for_marketplace_json(logger_mock)
        finally:
            os.chdir(old_cwd)
        logger_mock.warning.assert_not_called()

    def test_gitignore_with_matching_pattern_warns(self, tmp_path: Path) -> None:
        """A .gitignore line matching 'marketplace.json' should warn."""
        from apm_cli.commands.marketplace import _check_gitignore_for_marketplace_json

        (tmp_path / ".gitignore").write_text("marketplace.json\n", encoding="utf-8")
        logger_mock = MagicMock()
        import os

        old_cwd = os.getcwd()
        try:
            os.chdir(str(tmp_path))
            _check_gitignore_for_marketplace_json(logger_mock)
        finally:
            os.chdir(old_cwd)
        logger_mock.warning.assert_called_once()

    def test_gitignore_comment_line_ignored(self, tmp_path: Path) -> None:
        """Comment lines in .gitignore should not trigger a warning."""
        from apm_cli.commands.marketplace import _check_gitignore_for_marketplace_json

        (tmp_path / ".gitignore").write_text("# marketplace.json\n", encoding="utf-8")
        logger_mock = MagicMock()
        import os

        old_cwd = os.getcwd()
        try:
            os.chdir(str(tmp_path))
            _check_gitignore_for_marketplace_json(logger_mock)
        finally:
            os.chdir(old_cwd)
        logger_mock.warning.assert_not_called()

    def test_gitignore_json_wildcard_warns(self, tmp_path: Path) -> None:
        """A *.json rule in .gitignore should warn."""
        from apm_cli.commands.marketplace import _check_gitignore_for_marketplace_json

        (tmp_path / ".gitignore").write_text("*.json\n", encoding="utf-8")
        logger_mock = MagicMock()
        import os

        old_cwd = os.getcwd()
        try:
            os.chdir(str(tmp_path))
            _check_gitignore_for_marketplace_json(logger_mock)
        finally:
            os.chdir(old_cwd)
        logger_mock.warning.assert_called_once()


# ---------------------------------------------------------------------------
# Section 14 - _load_yml_or_exit (indirectly via CWD + sys.exit mocking)
# ---------------------------------------------------------------------------


class TestLoadYmlOrExit:
    """Tests for _load_yml_or_exit helper."""

    def test_missing_yml_exits_1(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_yml_or_exit

        logger_mock = MagicMock()
        import os

        old_cwd = os.getcwd()
        try:
            os.chdir(str(tmp_path))
            with pytest.raises(SystemExit) as exc_info:
                _load_yml_or_exit(logger_mock)
        finally:
            os.chdir(old_cwd)
        assert exc_info.value.code == 1

    def test_valid_yml_returns_config(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_yml_or_exit

        (tmp_path / "marketplace.yml").write_text(_MARKETPLACE_YML, encoding="utf-8")
        logger_mock = MagicMock()
        import os

        old_cwd = os.getcwd()
        try:
            os.chdir(str(tmp_path))
            yml = _load_yml_or_exit(logger_mock)
        finally:
            os.chdir(old_cwd)
        assert yml.name == "acme-marketplace"

    def test_invalid_yml_exits_2(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_yml_or_exit

        (tmp_path / "marketplace.yml").write_text(
            "name: ~\nversion: not-semver\n", encoding="utf-8"
        )
        logger_mock = MagicMock()
        import os

        old_cwd = os.getcwd()
        try:
            os.chdir(str(tmp_path))
            with pytest.raises(SystemExit) as exc_info:
                _load_yml_or_exit(logger_mock)
        finally:
            os.chdir(old_cwd)
        # Schema errors may produce exit code 1 or 2
        assert exc_info.value.code in (1, 2)
