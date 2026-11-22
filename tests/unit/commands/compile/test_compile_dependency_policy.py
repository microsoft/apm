"""``apm compile`` applies the org dependency policy before it renders installed content."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from apm_cli.commands.compile.cli import (
    _dependency_policy_allows,
    _enforce_dependency_policy,
    _rendered_dependencies,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-policy-violation-subjects")]

_RESOLVE = "apm_cli.policy.outcome_routing.resolve_trust_policy"


class _Logger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def error(self, message, symbol="error"):
        self.lines.append(("error", message))

    def warning(self, message, symbol="warning"):
        self.lines.append(("warning", message))

    def refusal(self, message):
        self.lines.append(("refusal", message))

    def progress(self, message, symbol=None):
        self.lines.append(("progress", message))

    def verbose_detail(self, message):
        self.lines.append(("verbose", message))


def _project(tmp_path: Path, *, locked: bool = True) -> Path:
    lock = LockFile()
    if locked:
        lock.dependencies["acme/denied"] = LockedDependency(repo_url="acme/denied")
    lock.write(tmp_path / "apm.lock.yaml")
    return tmp_path


def _run(project: Path, enforcement: str) -> tuple[int | None, _Logger]:
    logger = _Logger()
    policy = SimpleNamespace(enforcement=enforcement)
    with (
        patch(_RESOLVE, return_value=policy),
        patch(
            "apm_cli.install.exec_gate.policy_denied_dependencies",
            return_value={"acme/denied": "org policy: denied by pattern"},
        ),
    ):
        try:
            _enforce_dependency_policy(logger, [project])
        except SystemExit as exc:
            return exc.code, logger
    return None, logger


def test_block_enforcement_refuses_and_names_the_dependency(tmp_path: Path) -> None:
    code, logger = _run(_project(tmp_path), "block")

    assert code == 1
    assert ("error", "acme/denied: org policy: denied by pattern") in logger.lines
    assert any(kind == "refusal" and "apm.yml" in text for kind, text in logger.lines)
    assert not (tmp_path / "AGENTS.md").exists()


def test_warn_enforcement_reports_and_continues(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = (project / "apm.lock.yaml").read_bytes()
    code, logger = _run(project, "warn")

    assert code is None
    assert logger.lines == [("warning", "acme/denied: org policy: denied by pattern")]
    assert (project / "apm.lock.yaml").read_bytes() == before, "the check is read-only"


def test_off_enforcement_is_silent(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = (project / "apm.lock.yaml").read_bytes()
    code, logger = _run(project, "off")

    assert (code, logger.lines) == (None, [])
    assert (project / "apm.lock.yaml").read_bytes() == before, "the check is read-only"


def test_a_project_without_installed_packages_never_resolves_policy(tmp_path: Path) -> None:
    logger = _Logger()
    with patch(_RESOLVE) as resolve:
        _enforce_dependency_policy(logger, [_project(tmp_path, locked=False)])

    resolve.assert_not_called()
    assert logger.lines == []
    assert not (tmp_path / "AGENTS.md").exists()


def test_installed_packages_are_judged_without_a_lock_row(tmp_path: Path) -> None:
    """A lost lock still renders apm.yml's installed dependencies; they are judged too."""
    (tmp_path / "apm.yml").write_text(
        yaml.safe_dump(
            {"name": "p", "version": "1.0.0", "dependencies": {"apm": ["acme/declared"]}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "apm_modules" / "acme" / "declared").mkdir(parents=True)
    (tmp_path / "apm_modules" / "acme" / "absent").mkdir(parents=True)
    lock = LockFile()
    lock.local_staged_files = ["apm_modules/bundle-x/.apm/instructions/a.instructions.md"]
    lock.write(tmp_path / "apm.lock.yaml")

    keys = [key for key, _ref in _rendered_dependencies(tmp_path, user_scope=False)]

    assert keys == ["_local/bundle-x", "acme/declared"]
    assert (tmp_path / "apm.yml").is_file()


def test_a_redirected_install_root_is_judged_too(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    _project(redirected)
    logger = _Logger()
    with (
        patch(_RESOLVE, return_value=SimpleNamespace(enforcement="block")),
        patch(
            "apm_cli.install.exec_gate.policy_denied_dependencies",
            side_effect=lambda subjects, _policy: {
                key: "org policy: denied" for key, _ in subjects
            },
        ),
    ):
        allowed = _dependency_policy_allows(logger, [source, redirected])

    assert allowed is False
    assert ("error", "acme/denied: org policy: denied") in logger.lines
    assert not (source / "AGENTS.md").exists()


def test_global_compile_stops_when_policy_refuses(tmp_path: Path, monkeypatch) -> None:
    from apm_cli.commands.compile import cli as compile_cli

    apm_home = tmp_path / "apm-home"
    (apm_home / "apm_modules").mkdir(parents=True)
    _project(apm_home)
    monkeypatch.setattr("apm_cli.core.scope.get_apm_dir", lambda scope=None: apm_home)
    logger = _Logger()
    with (
        patch(_RESOLVE, return_value=SimpleNamespace(enforcement="block")),
        patch(
            "apm_cli.install.exec_gate.policy_denied_dependencies",
            return_value={"acme/denied": "org policy: denied by pattern"},
        ),
        patch("apm_cli.compilation.compile_user_root_contexts") as render,
    ):
        code = compile_cli._handle_global_flag(dry_run=False, logger=logger)

    assert code == 1
    render.assert_not_called()
    assert ("error", "acme/denied: org policy: denied by pattern") in logger.lines
    assert not (apm_home / "AGENTS.md").exists()


def test_a_package_declared_in_one_root_and_installed_in_the_other_is_judged(
    tmp_path: Path,
) -> None:
    """--root: the source manifest declares it, the redirected root installed it, no lock."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "apm.yml").write_text(
        yaml.safe_dump({"name": "p", "version": "1.0.0", "dependencies": {"apm": ["acme/denied"]}}),
        encoding="utf-8",
    )
    redirected = tmp_path / "redirected"
    (redirected / "apm_modules" / "acme" / "denied").mkdir(parents=True)
    logger = _Logger()
    with (
        patch(_RESOLVE, return_value=SimpleNamespace(enforcement="block")),
        patch(
            "apm_cli.install.exec_gate.policy_denied_dependencies",
            side_effect=lambda subjects, _policy: {
                key: "org policy: denied" for key, _ in subjects
            },
        ),
    ):
        allowed = _dependency_policy_allows(logger, [source, redirected])

    assert allowed is False
    assert ("error", "acme/denied: org policy: denied") in logger.lines
    assert not (source / "AGENTS.md").exists()


def test_a_package_a_pre_compile_script_installs_is_judged_before_rendering(
    tmp_path: Path, monkeypatch
) -> None:
    """The early check passed on an empty project; the script then installed a denied package."""
    from apm_cli.commands.compile import cli as compile_cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text("name: p\nversion: 1.0.0\n", encoding="utf-8")

    class _Runner:
        def scripts_for_event(self, _event):
            return []

        def fire(self, _event, _payload):
            _project(tmp_path)  # the script installs a package the org denies

    logger = _Logger()
    with (
        patch("apm_cli.core.lifecycle_scripts.build_runner_from_context", return_value=_Runner()),
        patch(_RESOLVE, return_value=SimpleNamespace(enforcement="block")),
        patch(
            "apm_cli.install.exec_gate.policy_denied_dependencies",
            return_value={"acme/denied": "org policy: denied by pattern"},
        ),
        pytest.raises(SystemExit) as exit_info,
    ):
        compile_cli._run_compilation(
            logger,
            None,
            "AGENTS.md",
            False,
            False,
            None,
            True,
            False,
            False,
            False,
            True,
            False,
            source_root=tmp_path,
            run_scripts=True,
        )

    assert exit_info.value.code == 1
    assert ("error", "acme/denied: org policy: denied by pattern") in logger.lines
    assert not (tmp_path / "AGENTS.md").exists()


def _deny_refs_named_denied(subjects, _policy):
    return {
        key: "org policy: denied by pattern"
        for key, ref in subjects
        if "denied" in str(getattr(ref, "repo_url", ""))
    }


def _replaced_lock(root: Path) -> None:
    """A lock whose owner/upstream row resolves, by a replace: rule, to a denied fork."""
    from apm_cli.models.dependency.reference import DependencyReference
    from apm_cli.models.dependency.replacement import (
        ReplacementBinding,
        parse_replace_block,
        project_effective_reference,
    )

    block = [
        {
            "for": {"git": "https://github.com/owner/upstream"},
            "with": {"git": "https://github.com/denied-fork/upstream", "ref": "fixed"},
        }
    ]
    original = DependencyReference(repo_url="owner/upstream", host="github.com", reference="v1")
    rule = parse_replace_block(block)[0]
    binding = ReplacementBinding(original, rule)
    binding.record(original, None, False)
    effective = project_effective_reference(original, rule)
    effective.replacement = binding
    lock = LockFile()
    lock.replacements = block
    lock.add_dependency(
        LockedDependency.from_dependency_ref(
            dep_ref=effective, resolved_commit="a" * 40, depth=1, resolved_by=None
        )
    )
    lock.write(root / "apm.lock.yaml")


def test_the_lock_derived_source_is_judged_not_the_declared_one(tmp_path: Path) -> None:
    """--root with a replace: rule: the lock's effective source wins over the manifest entry."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "apm.yml").write_text(
        yaml.safe_dump(
            {"name": "p", "version": "1.0.0", "dependencies": {"apm": ["owner/upstream"]}}
        ),
        encoding="utf-8",
    )
    redirected = tmp_path / "redirected"
    (redirected / "apm_modules" / "owner" / "upstream").mkdir(parents=True)
    _replaced_lock(redirected)
    logger = _Logger()
    with (
        patch(_RESOLVE, return_value=SimpleNamespace(enforcement="block")),
        patch(
            "apm_cli.install.exec_gate.policy_denied_dependencies",
            side_effect=_deny_refs_named_denied,
        ),
    ):
        allowed = _dependency_policy_allows(logger, [source, redirected])

    assert allowed is False
    assert ("error", "owner/upstream: org policy: denied by pattern") in logger.lines
    assert (redirected / "apm.lock.yaml").is_file()


def test_a_script_that_swaps_a_locked_source_is_judged_before_rendering(
    tmp_path: Path, monkeypatch
) -> None:
    """Same key, new source: the recheck judges the current inventory, not the keys seen."""
    from apm_cli.commands.compile import cli as compile_cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text("name: p\nversion: 1.0.0\n", encoding="utf-8")
    lock = LockFile()
    lock.dependencies["owner/upstream"] = LockedDependency(repo_url="owner/upstream")
    lock.write(tmp_path / "apm.lock.yaml")

    class _Runner:
        def scripts_for_event(self, _event):
            return []

        def fire(self, _event, _payload):
            _replaced_lock(tmp_path)  # same key, now resolving to a denied fork

    logger = _Logger()
    with (
        patch("apm_cli.core.lifecycle_scripts.build_runner_from_context", return_value=_Runner()),
        patch(_RESOLVE, return_value=SimpleNamespace(enforcement="block")),
        patch(
            "apm_cli.install.exec_gate.policy_denied_dependencies",
            side_effect=_deny_refs_named_denied,
        ),
        pytest.raises(SystemExit) as exit_info,
    ):
        compile_cli._run_compilation(
            logger,
            None,
            "AGENTS.md",
            False,
            False,
            None,
            True,
            False,
            False,
            False,
            True,
            False,
            source_root=tmp_path,
            run_scripts=True,
        )

    assert exit_info.value.code == 1
    assert ("error", "owner/upstream: org policy: denied by pattern") in logger.lines
    assert not (tmp_path / "AGENTS.md").exists()


def test_warn_enforcement_reports_each_package_once_per_command(tmp_path: Path) -> None:
    project = _project(tmp_path)
    logger = _Logger()
    with (
        patch(_RESOLVE, return_value=SimpleNamespace(enforcement="warn")),
        patch(
            "apm_cli.install.exec_gate.policy_denied_dependencies",
            return_value={"acme/denied": "org policy: denied by pattern"},
        ),
    ):
        assert _dependency_policy_allows(logger, [project]) is True
        assert _dependency_policy_allows(logger, [project]) is True

    warned = [line for line in logger.lines if line[0] == "warning"]
    assert warned == [("warning", "acme/denied: org policy: denied by pattern")]
    assert (project / "apm.lock.yaml").is_file()


def test_the_policy_is_resolved_once_per_command(tmp_path: Path) -> None:
    """The recheck under the lock re-reads the inventory, not the policy."""
    project = _project(tmp_path)
    logger = _Logger()
    with (
        patch(_RESOLVE, return_value=SimpleNamespace(enforcement="block")) as resolve,
        patch("apm_cli.install.exec_gate.policy_denied_dependencies", return_value={}),
    ):
        assert _dependency_policy_allows(logger, [project]) is True
        assert _dependency_policy_allows(logger, [project]) is True

    assert resolve.call_count == 1
    assert (project / "apm.lock.yaml").is_file()


def test_a_watch_session_sees_an_allow_to_deny_policy_change(tmp_path: Path, monkeypatch) -> None:
    """Each watch recompile resolves the org policy again; a deny stops the next render."""
    from unittest.mock import MagicMock

    from apm_cli.commands.compile.watcher import APMFileHandler

    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    logger = MagicMock()
    handler = APMFileHandler(
        output="AGENTS.md", chatmode=None, no_links=False, dry_run=True, logger=logger
    )
    policies = iter(
        [SimpleNamespace(enforcement="block", deny=False)]
        + [SimpleNamespace(enforcement="block", deny=True)] * 3
    )
    compiler = MagicMock()
    compiler.return_value.compile.return_value = SimpleNamespace(
        success=True, output_path="AGENTS.md", errors=[]
    )
    with (
        patch(_RESOLVE, side_effect=lambda *_a, **_k: next(policies)),
        patch(
            "apm_cli.install.exec_gate.policy_denied_dependencies",
            side_effect=lambda subjects, policy: (
                {key: "org policy: denied by pattern" for key, _ in subjects} if policy.deny else {}
            ),
        ),
        patch("apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml"),
        patch("apm_cli.commands.compile.watcher.AgentsCompiler", compiler),
    ):
        handler._recompile("a.instructions.md")
        handler._recompile("a.instructions.md")

    assert compiler.return_value.compile.call_count == 1, "the second change is refused"
    logger.error.assert_any_call("acme/denied: org policy: denied by pattern")
    assert not (tmp_path / "AGENTS.md").exists()
