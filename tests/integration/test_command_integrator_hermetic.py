from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import frontmatter
import pytest
import yaml

from apm_cli.integration.command_integrator import (
    CommandIntegrator,
    _extract_input_names,
    _is_valid_input_name,
)
from apm_cli.utils.path_security import PathTraversalError


@pytest.fixture(autouse=True)
def _force_yaml_pure_python(monkeypatch):
    """Force frontmatter to use PyYAML's pure-Python SafeLoader and SafeDumper.

    ``frontmatter.default_handlers`` imports ``SafeLoader``/``SafeDumper`` from
    ``yaml.cyaml`` (the C extension). Coverage's C tracer can corrupt the
    C-extension internal state when multiple C-extension modules are
    simultaneously instrumented. Substituting the pure-Python equivalents
    avoids this without changing any production code.
    """
    import frontmatter.default_handlers as _fdh

    monkeypatch.setattr(_fdh, "SafeLoader", yaml.SafeLoader)
    monkeypatch.setattr(_fdh, "SafeDumper", yaml.SafeDumper)


def _make_package_info(tmp_path: Path) -> MagicMock:
    pkg_dir = tmp_path / "apm_modules" / "test-pkg"
    prompts_dir = pkg_dir / ".apm" / "prompts"
    prompts_dir.mkdir(parents=True)

    package_info = MagicMock()
    package_info.install_path = pkg_dir
    package_info.package = MagicMock()
    package_info.package.name = "test-pkg"
    package_info.resolved_reference = None
    return package_info


def _write_prompt(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_target(
    *,
    name: str = "claude",
    auto_create: bool = True,
    root_dir: str = ".claude",
    format_id: str = "claude_command",
    deploy_root: str | None = None,
    extension: str = ".md",
) -> MagicMock:
    mapping = MagicMock()
    mapping.deploy_root = deploy_root
    mapping.subdir = "commands"
    mapping.format_id = format_id
    mapping.extension = extension

    target = MagicMock()
    target.name = name
    target.auto_create = auto_create
    target.root_dir = root_dir
    target.primitives = {"commands": mapping}
    target.hooks_config_display = None
    return target


@pytest.fixture(autouse=True)
def _suppress_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apm_cli.utils.console._get_console", lambda: None)


class TestIsValidInputName:
    def test_accepts_simple_name(self) -> None:
        assert _is_valid_input_name("feature_name") is True

    def test_accepts_hyphen_and_digits_after_prefix(self) -> None:
        assert _is_valid_input_name("feature-name2") is True

    def test_rejects_name_starting_with_digit(self) -> None:
        assert _is_valid_input_name("1feature") is False

    def test_rejects_empty_name(self) -> None:
        assert _is_valid_input_name("") is False

    def test_rejects_yaml_significant_characters(self) -> None:
        assert _is_valid_input_name("feature:name") is False


class TestExtractInputNames:
    def test_none_returns_empty_lists(self) -> None:
        assert _extract_input_names(None) == ([], [])

    def test_list_extracts_strings_and_dict_keys(self) -> None:
        valid, rejected = _extract_input_names(["feature_name", {"feature-description": "desc"}])

        assert valid == ["feature_name", "feature-description"]
        assert rejected == []

    def test_list_rejects_invalid_entries(self) -> None:
        valid, rejected = _extract_input_names(["", "bad:name", 7, {1: "desc"}])

        assert valid == []
        assert rejected == ["bad:name", "7", "1"]

    def test_string_input_extracts_single_name(self) -> None:
        assert _extract_input_names("feature_name") == (["feature_name"], [])

    def test_dict_input_uses_keys_only(self) -> None:
        valid, rejected = _extract_input_names({"feature_name": "desc", "bad:name": "x"})

        assert valid == ["feature_name"]
        assert rejected == ["bad:name"]

    def test_whitespace_entries_are_silently_dropped(self) -> None:
        valid, rejected = _extract_input_names(["  ", "\t", "good_name"])

        assert valid == ["good_name"]
        assert rejected == []

    def test_unsupported_scalar_type_returns_empty_valid_and_rejected(self) -> None:
        assert _extract_input_names(42) == ([], [])


class TestShouldEmitPassthroughNotice:
    def test_returns_false_without_dropped_keys(self) -> None:
        integrator = CommandIntegrator()

        assert (
            integrator._should_emit_passthrough_notice(
                "cursor",
                "claude_command",
                had_dropped_keys=False,
            )
            is False
        )

    def test_returns_false_for_non_claude_command_format(self) -> None:
        integrator = CommandIntegrator()

        assert (
            integrator._should_emit_passthrough_notice(
                "cursor",
                "gemini_command",
                had_dropped_keys=True,
            )
            is False
        )

    def test_returns_false_for_claude_target(self) -> None:
        integrator = CommandIntegrator()

        assert (
            integrator._should_emit_passthrough_notice(
                "claude",
                "claude_command",
                had_dropped_keys=True,
            )
            is False
        )

    def test_returns_true_first_time_for_cursor(self) -> None:
        integrator = CommandIntegrator()

        assert (
            integrator._should_emit_passthrough_notice(
                "cursor",
                "claude_command",
                had_dropped_keys=True,
            )
            is True
        )
        assert "cursor" in integrator._passthrough_notified

    def test_returns_false_after_target_already_notified(self) -> None:
        integrator = CommandIntegrator()
        integrator._passthrough_notified.add("cursor")

        assert (
            integrator._should_emit_passthrough_notice(
                "cursor",
                "claude_command",
                had_dropped_keys=True,
            )
            is False
        )

    def test_tracks_targets_independently(self) -> None:
        integrator = CommandIntegrator()

        assert (
            integrator._should_emit_passthrough_notice(
                "cursor",
                "claude_command",
                had_dropped_keys=True,
            )
            is True
        )
        assert (
            integrator._should_emit_passthrough_notice(
                "opencode",
                "claude_command",
                had_dropped_keys=True,
            )
            is True
        )


class TestTransformPromptToCommand:
    def test_uses_prompt_filename_without_suffix(self, tmp_path: Path) -> None:
        prompt = _write_prompt(
            tmp_path / "feature.prompt.md",
            "---\ndescription: Demo\n---\nRun it.\n",
        )

        command_name, post, warnings, dropped = CommandIntegrator()._transform_prompt_to_command(
            prompt
        )

        assert command_name == "feature"
        assert post.metadata["description"] == "Demo"
        assert warnings == []
        assert dropped == []

    def test_falls_back_to_stem_for_non_prompt_suffix(self, tmp_path: Path) -> None:
        prompt = _write_prompt(
            tmp_path / "feature.md",
            "---\ndescription: Demo\n---\nRun it.\n",
        )

        command_name, _, _, _ = CommandIntegrator()._transform_prompt_to_command(prompt)

        assert command_name == "feature"

    def test_maps_supported_frontmatter_and_generates_arguments(self, tmp_path: Path) -> None:
        prompt = _write_prompt(
            tmp_path / "plan.prompt.md",
            "---\ndescription: My command\nallowed-tools: [Bash]\nmodel: opus\ninput:\n  - feature_name\n  - feature_description\n---\nUse ${{input:feature_name}} then ${{input:feature_description}}.\n",
        )

        _, post, warnings, dropped = CommandIntegrator()._transform_prompt_to_command(prompt)

        assert post.metadata == {
            "description": "My command",
            "allowed-tools": ["Bash"],
            "model": "opus",
            "arguments": ["feature_name", "feature_description"],
            "argument-hint": "<feature_name> <feature_description>",
        }
        assert post.content.strip() == "Use $feature_name then $feature_description."
        assert warnings == []
        assert dropped == []

    def test_maps_camel_case_aliases(self, tmp_path: Path) -> None:
        prompt = _write_prompt(
            tmp_path / "plan.prompt.md",
            "---\ndescription: My command\nallowedTools: [Bash]\nargumentHint: feature\n---\nRun it.\n",
        )

        _, post, _, _ = CommandIntegrator()._transform_prompt_to_command(prompt)

        assert post.metadata["allowed-tools"] == ["Bash"]
        assert post.metadata["argument-hint"] == "feature"

    def test_keeps_explicit_argument_hint(self, tmp_path: Path) -> None:
        prompt = _write_prompt(
            tmp_path / "plan.prompt.md",
            "---\nargument-hint: custom\ninput:\n  - feature_name\n---\nUse ${{input:feature_name}}.\n",
        )

        _, post, _, _ = CommandIntegrator()._transform_prompt_to_command(prompt)

        assert post.metadata["arguments"] == ["feature_name"]
        assert post.metadata["argument-hint"] == "custom"

    def test_reports_rejected_input_names(self, tmp_path: Path) -> None:
        prompt = _write_prompt(
            tmp_path / "plan.prompt.md",
            "---\ninput:\n  - feature_name\n  - bad:name\n  - 3\n---\nRun it.\n",
        )

        _, post, warnings, _ = CommandIntegrator()._transform_prompt_to_command(prompt)

        assert post.metadata["arguments"] == ["feature_name"]
        assert len(warnings) == 1
        assert "bad:name" in warnings[0]
        assert "3" in warnings[0]

    def test_computes_sorted_dropped_keys(self, tmp_path: Path) -> None:
        prompt = _write_prompt(
            tmp_path / "plan.prompt.md",
            "---\nauthor: Sergio\ndescription: My command\nmcp: true\n---\nRun it.\n",
        )

        _, _, _, dropped = CommandIntegrator()._transform_prompt_to_command(prompt)

        assert dropped == ["author", "mcp"]

    def test_leaves_content_unchanged_when_no_input_names_exist(self, tmp_path: Path) -> None:
        prompt = _write_prompt(
            tmp_path / "plan.prompt.md",
            "---\ndescription: My command\n---\nUse ${{input:feature_name}} literally.\n",
        )

        _, post, _, _ = CommandIntegrator()._transform_prompt_to_command(prompt)

        assert post.content.strip() == "Use ${{input:feature_name}} literally."


class TestIntegrateCommand:
    def test_emits_dropped_key_warning(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        source = _write_prompt(
            tmp_path / "source" / "audit.prompt.md",
            "---\nauthor: Sergio\ndescription: Audit\n---\nRun it.\n",
        )
        target = tmp_path / ".claude" / "commands" / "audit.md"
        diagnostics = MagicMock()
        verdict = MagicMock(
            has_critical=False,
            has_findings=False,
            critical_count=0,
            warning_count=0,
        )
        integrator = CommandIntegrator()

        with (
            patch.object(integrator, "resolve_links", return_value=("Run it.\n", 0)),
            patch(
                "apm_cli.integration.command_integrator.SecurityGate.scan_text",
                return_value=verdict,
            ),
        ):
            links_resolved, written, had_dropped = integrator.integrate_command(
                source,
                target,
                package_info,
                source,
                diagnostics=diagnostics,
                target_name="cursor",
            )

        assert (links_resolved, written, had_dropped) == (0, True, True)
        diagnostics.warn.assert_called_once()
        assert "frontmatter keys not supported" in diagnostics.warn.call_args.kwargs["message"]
        assert "author" in diagnostics.warn.call_args.kwargs["message"]

    def test_emits_mapped_arguments_info(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        source = _write_prompt(
            tmp_path / "source" / "audit.prompt.md",
            "---\ninput:\n  - feature_name\n---\nUse ${{input:feature_name}}.\n",
        )
        target = tmp_path / ".claude" / "commands" / "audit.md"
        diagnostics = MagicMock()
        verdict = MagicMock(
            has_critical=False,
            has_findings=False,
            critical_count=0,
            warning_count=0,
        )
        integrator = CommandIntegrator()

        with (
            patch.object(integrator, "resolve_links", return_value=("Use $feature_name.\n", 0)),
            patch(
                "apm_cli.integration.command_integrator.SecurityGate.scan_text",
                return_value=verdict,
            ),
        ):
            integrator.integrate_command(
                source,
                target,
                package_info,
                source,
                diagnostics=diagnostics,
            )

        diagnostics.info.assert_called_once()
        assert "Mapped input -> command arguments" in diagnostics.info.call_args.kwargs["message"]

    def test_blocks_write_on_critical_security_verdict(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        source = _write_prompt(
            tmp_path / "source" / "audit.prompt.md",
            "---\ndescription: Audit\n---\nRun it.\n",
        )
        target = tmp_path / ".claude" / "commands" / "audit.md"
        diagnostics = MagicMock()
        verdict = MagicMock(
            has_critical=True,
            has_findings=True,
            critical_count=1,
            warning_count=2,
        )
        integrator = CommandIntegrator()

        with (
            patch.object(integrator, "resolve_links", return_value=("Run it.\n", 0)),
            patch(
                "apm_cli.integration.command_integrator.SecurityGate.scan_text",
                return_value=verdict,
            ),
        ):
            result = integrator.integrate_command(
                source,
                target,
                package_info,
                source,
                diagnostics=diagnostics,
            )

        assert result == (0, False, False)
        assert target.exists() is False
        diagnostics.security.assert_called_once()
        assert diagnostics.security.call_args.kwargs["severity"] == "critical"

    def test_writes_file_and_emits_security_warning(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        source = _write_prompt(
            tmp_path / "source" / "audit.prompt.md",
            "---\ndescription: Audit\n---\nRun it.\n",
        )
        target = tmp_path / ".claude" / "commands" / "audit.md"
        diagnostics = MagicMock()
        verdict = MagicMock(
            has_critical=False,
            has_findings=True,
            critical_count=0,
            warning_count=2,
        )
        integrator = CommandIntegrator()

        with (
            patch.object(integrator, "resolve_links", return_value=("Resolved body\n", 3)),
            patch(
                "apm_cli.integration.command_integrator.SecurityGate.scan_text",
                return_value=verdict,
            ),
        ):
            result = integrator.integrate_command(
                source,
                target,
                package_info,
                source,
                diagnostics=diagnostics,
            )

        assert result == (3, True, False)
        assert target.exists() is True
        written = frontmatter.load(target)
        assert written.content.strip() == "Resolved body"
        diagnostics.security.assert_called_once()
        assert diagnostics.security.call_args.kwargs["severity"] == "warning"

    def test_scan_oserror_becomes_warning_and_still_writes(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        source = _write_prompt(
            tmp_path / "source" / "audit.prompt.md",
            "---\ndescription: Audit\n---\nRun it.\n",
        )
        target = tmp_path / ".claude" / "commands" / "audit.md"
        diagnostics = MagicMock()
        integrator = CommandIntegrator()

        with (
            patch.object(integrator, "resolve_links", return_value=("Run it.\n", 1)),
            patch(
                "apm_cli.integration.command_integrator.SecurityGate.scan_text",
                side_effect=OSError("scan failed"),
            ),
        ):
            result = integrator.integrate_command(
                source,
                target,
                package_info,
                source,
                diagnostics=diagnostics,
            )

        assert result == (1, True, False)
        assert target.exists() is True
        assert any(
            "security scan skipped due to scan error" in call.kwargs["message"]
            for call in diagnostics.warn.call_args_list
        )

    def test_logs_security_warning_without_diagnostics(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        source = _write_prompt(
            tmp_path / "source" / "audit.prompt.md",
            "---\ndescription: Audit\n---\nRun it.\n",
        )
        target = tmp_path / ".claude" / "commands" / "audit.md"
        verdict = MagicMock(
            has_critical=False,
            has_findings=True,
            critical_count=0,
            warning_count=1,
        )
        integrator = CommandIntegrator()

        with (
            patch.object(integrator, "resolve_links", return_value=("Run it.\n", 0)),
            patch(
                "apm_cli.integration.command_integrator.SecurityGate.scan_text",
                return_value=verdict,
            ),
            patch("apm_cli.integration.command_integrator.logger.warning") as mock_warning,
        ):
            integrator.integrate_command(source, target, package_info, source, diagnostics=None)

        mock_warning.assert_called_once()

    def test_surfaces_rejected_input_name_warning(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        source = _write_prompt(
            tmp_path / "source" / "audit.prompt.md",
            "---\ninput:\n  - bad:name\n---\nRun it.\n",
        )
        target = tmp_path / ".claude" / "commands" / "audit.md"
        diagnostics = MagicMock()
        verdict = MagicMock(
            has_critical=False,
            has_findings=False,
            critical_count=0,
            warning_count=0,
        )
        integrator = CommandIntegrator()

        with (
            patch.object(integrator, "resolve_links", return_value=("Run it.\n", 0)),
            patch(
                "apm_cli.integration.command_integrator.SecurityGate.scan_text",
                return_value=verdict,
            ),
        ):
            integrator.integrate_command(
                source,
                target,
                package_info,
                source,
                diagnostics=diagnostics,
            )

        assert any(
            call.kwargs["message"].startswith("input: rejected")
            for call in diagnostics.warn.call_args_list
        )

    def test_writes_transformed_frontmatter_to_disk(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        source = _write_prompt(
            tmp_path / "source" / "audit.prompt.md",
            "---\ndescription: Audit\ninput:\n  - feature_name\n---\nUse ${{input:feature_name}}.\n",
        )
        target = tmp_path / ".claude" / "commands" / "audit.md"
        verdict = MagicMock(
            has_critical=False,
            has_findings=False,
            critical_count=0,
            warning_count=0,
        )
        integrator = CommandIntegrator()

        with (
            patch.object(integrator, "resolve_links", return_value=("Use $feature_name.\n", 0)),
            patch(
                "apm_cli.integration.command_integrator.SecurityGate.scan_text",
                return_value=verdict,
            ),
        ):
            integrator.integrate_command(source, target, package_info, source)

        written = frontmatter.load(target)
        assert written.metadata["arguments"] == ["feature_name"]
        assert written.metadata["argument-hint"] == "<feature_name>"
        assert written.content.strip() == "Use $feature_name."


class TestIntegrateCommandsForTarget:
    def test_returns_empty_result_when_mapping_missing(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        target = MagicMock()
        target.primitives = {}

        result = CommandIntegrator().integrate_commands_for_target(target, package_info, tmp_path)

        assert result.files_integrated == 0
        assert result.files_skipped == 0
        assert result.links_resolved == 0

    def test_skips_when_target_root_missing_and_auto_create_false(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        target = _make_target(name="claude", auto_create=False)
        diagnostics = MagicMock()

        result = CommandIntegrator().integrate_commands_for_target(
            target,
            package_info,
            tmp_path,
            diagnostics=diagnostics,
        )

        assert result.files_integrated == 0
        diagnostics.info.assert_called_once()
        assert "Skipped .claude/commands/" in diagnostics.info.call_args.kwargs["message"]

    def test_returns_empty_result_when_no_prompt_files_exist(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        target = _make_target()
        (tmp_path / ".claude").mkdir()

        result = CommandIntegrator().integrate_commands_for_target(target, package_info, tmp_path)

        assert result.files_integrated == 0
        assert result.files_skipped == 0

    def test_skips_workflow_shaped_prompts(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        prompt = _write_prompt(
            package_info.install_path / ".apm" / "prompts" / "workflow.prompt.md",
            "---\ninterval: manual\n---\nRun it.\n",
        )
        target = _make_target()
        diagnostics = MagicMock()
        integrator = CommandIntegrator()

        with patch.object(integrator, "init_link_resolver"):
            result = integrator.integrate_commands_for_target(
                target,
                package_info,
                tmp_path,
                diagnostics=diagnostics,
            )

        assert prompt.exists() is True
        assert result.files_skipped == 1
        assert result.files_integrated == 0
        diagnostics.info.assert_not_called()

    def test_rejects_invalid_command_filename(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        prompt = _write_prompt(
            package_info.install_path / ".apm" / "prompts" / "bad.prompt.md",
            "---\ndescription: Demo\n---\nRun it.\n",
        )
        target = _make_target()
        diagnostics = MagicMock()
        integrator = CommandIntegrator()

        with (
            patch.object(integrator, "init_link_resolver"),
            patch(
                "apm_cli.integration.command_integrator.validate_path_segments",
                side_effect=PathTraversalError("command filename"),
            ),
        ):
            result = integrator.integrate_commands_for_target(
                target,
                package_info,
                tmp_path,
                diagnostics=diagnostics,
            )

        assert prompt.exists() is True
        assert result.files_skipped == 1
        assert result.files_integrated == 0
        diagnostics.warn.assert_called_once()
        assert "Rejected command filename" in diagnostics.warn.call_args.kwargs["message"]

    def test_rejects_target_path_outside_commands_dir(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        _write_prompt(
            package_info.install_path / ".apm" / "prompts" / "good.prompt.md",
            "---\ndescription: Demo\n---\nRun it.\n",
        )
        target = _make_target()
        diagnostics = MagicMock()
        integrator = CommandIntegrator()

        with (
            patch.object(integrator, "init_link_resolver"),
            patch(
                "apm_cli.integration.command_integrator.ensure_path_within",
                side_effect=PathTraversalError("outside"),
            ),
        ):
            result = integrator.integrate_commands_for_target(
                target,
                package_info,
                tmp_path,
                diagnostics=diagnostics,
            )

        assert result.files_skipped == 1
        assert result.files_integrated == 0
        diagnostics.warn.assert_called_once()
        assert "Rejected command target path" in diagnostics.warn.call_args.kwargs["message"]

    def test_counts_adopted_skips(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        _write_prompt(
            package_info.install_path / ".apm" / "prompts" / "good.prompt.md",
            "---\ndescription: Demo\n---\nRun it.\n",
        )
        target = _make_target()
        integrator = CommandIntegrator()

        def _adopt(
            target_path: Path,
            source_file: Path,
            rel_path: str,
            managed_files: set[str] | None,
            force: bool,
            diagnostics: MagicMock,
            target_paths: list[Path],
        ) -> tuple[bool, bool]:
            target_paths.append(target_path)
            return (True, True)

        with (
            patch.object(integrator, "init_link_resolver"),
            patch.object(integrator, "_check_adopt_or_skip", side_effect=_adopt),
        ):
            result = integrator.integrate_commands_for_target(target, package_info, tmp_path)

        assert result.files_adopted == 1
        assert result.files_skipped == 0
        assert len(result.target_paths) == 1

    def test_counts_non_adopted_skip(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        _write_prompt(
            package_info.install_path / ".apm" / "prompts" / "good.prompt.md",
            "---\ndescription: Demo\n---\nRun it.\n",
        )
        target = _make_target()
        integrator = CommandIntegrator()

        with (
            patch.object(integrator, "init_link_resolver"),
            patch.object(integrator, "_check_adopt_or_skip", return_value=(True, False)),
        ):
            result = integrator.integrate_commands_for_target(target, package_info, tmp_path)

        assert result.files_skipped == 1
        assert result.files_adopted == 0

    def test_dispatches_gemini_writer(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        _write_prompt(
            package_info.install_path / ".apm" / "prompts" / "good.prompt.md",
            "---\ndescription: Demo\n---\nRun it.\n",
        )
        target = _make_target(
            name="gemini", root_dir=".gemini", format_id="gemini_command", extension=".toml"
        )
        integrator = CommandIntegrator()

        with (
            patch.object(integrator, "init_link_resolver"),
            patch.object(integrator, "_check_adopt_or_skip", return_value=(False, False)),
            patch.object(integrator, "_write_gemini_command") as mock_write,
        ):
            result = integrator.integrate_commands_for_target(target, package_info, tmp_path)

        mock_write.assert_called_once()
        assert result.files_integrated == 1
        assert result.links_resolved == 0

    def test_full_flow_emits_passthrough_notice_when_keys_dropped(self, tmp_path: Path) -> None:
        package_info = _make_package_info(tmp_path)
        _write_prompt(
            package_info.install_path / ".apm" / "prompts" / "good.prompt.md",
            "---\ndescription: Demo\nauthor: Sergio\n---\nRun it.\n",
        )
        target = _make_target(name="cursor", root_dir=".cursor")
        diagnostics = MagicMock()
        integrator = CommandIntegrator()

        with (
            patch.object(integrator, "init_link_resolver"),
            patch.object(integrator, "_check_adopt_or_skip", return_value=(False, False)),
            patch.object(
                integrator, "integrate_command", return_value=(2, True, True)
            ) as mock_integrate,
        ):
            result = integrator.integrate_commands_for_target(
                target,
                package_info,
                tmp_path,
                diagnostics=diagnostics,
            )

        mock_integrate.assert_called_once()
        assert result.files_integrated == 1
        assert result.links_resolved == 2
        assert result.target_paths == [tmp_path / ".cursor" / "commands" / "good.md"]
        assert any(
            "Claude-compatible frontmatter" in call.kwargs["message"]
            for call in diagnostics.info.call_args_list
        )
