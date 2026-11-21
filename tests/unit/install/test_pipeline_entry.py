"""install-entry-routing owner: preview recording, preview entry and pipeline options (#4121)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock

import click
import pytest

from apm_cli.install.errors import (
    AuthenticationError,
    FrozenInstallError,
    InstallFailureAlreadyRendered,
    PolicyViolationError,
    PreviewIncomplete,
)
from apm_cli.install.insecure_policy import InsecureDependencyPolicyError
from apm_cli.install.pipeline_entry import (
    PipelinePreview,
    install_pipeline_options,
    pipeline_preview,
    record_preview_resolution,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-install-entry-routing")]


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(preview_errors=[], preview_declaration_errors=[])


def _graph(*, resolution=(), unavailable=(), declaration=(), circular=()) -> SimpleNamespace:
    return SimpleNamespace(
        resolution_errors=list(resolution),
        dependency_tree=SimpleNamespace(unavailable_subtrees=list(unavailable)),
        declaration_errors=list(declaration),
        circular_dependencies=list(circular),
    )


def test_clean_graph_records_nothing_and_continues() -> None:
    ctx = _ctx()
    record_preview_resolution(ctx, _graph())
    assert (ctx.preview_errors, ctx.preview_declaration_errors) == ([], [])


def test_unavailable_subtrees_and_declaration_errors_are_recorded_without_stopping() -> None:
    ctx = _ctx()
    record_preview_resolution(ctx, _graph(unavailable=["acme/gone"], declaration=["bad key"]))
    assert ctx.preview_errors == ["acme/gone"]
    assert ctx.preview_declaration_errors == ["bad key"]


def test_resolution_errors_are_recorded_then_stop_the_preview() -> None:
    ctx = _ctx()
    with pytest.raises(PreviewIncomplete):
        record_preview_resolution(ctx, _graph(resolution=["no tag v9"], unavailable=["acme/x"]))
    assert ctx.preview_errors == ["no tag v9", "acme/x"]


def test_circular_dependencies_add_one_error_and_stop_the_preview() -> None:
    ctx = _ctx()
    with pytest.raises(PreviewIncomplete):
        record_preview_resolution(ctx, _graph(circular=[("a", "b")]))
    assert ctx.preview_errors == ["Circular dependencies prevent a complete install preview"]


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError("auth"),
        FrozenInstallError("frozen"),
        InsecureDependencyPolicyError("insecure"),
        InstallFailureAlreadyRendered(),
        PolicyViolationError("policy"),
        click.UsageError("usage"),
    ],
)
def test_refusals_propagate_out_of_the_preview(error: BaseException) -> None:
    def run() -> None:
        raise error

    with pytest.raises(type(error)):
        pipeline_preview(run, Mock())


def test_an_unreachable_source_becomes_an_incomplete_preview() -> None:
    logger = Mock()

    def run() -> None:
        raise OSError("host unreachable")

    preview = pipeline_preview(run, logger)

    assert preview == PipelinePreview(
        dependencies=(),
        resolved_dependencies=(),
        mcp_dependencies=None,
        errors=("host unreachable",),
    )
    logger.verbose_detail.assert_called_once_with("Dry-run resolution stopped: host unreachable")


def test_an_empty_error_message_falls_back_to_the_exception_type() -> None:
    def run() -> None:
        raise RuntimeError()

    preview = pipeline_preview(run, Mock())

    assert preview is not None and preview.errors == ("RuntimeError",)


def test_the_pipeline_preview_is_returned_and_anything_else_is_none() -> None:
    built = PipelinePreview(dependencies=(), resolved_dependencies=(), mcp_dependencies=())
    assert pipeline_preview(lambda: SimpleNamespace(preview=built), Mock()) is built
    assert pipeline_preview(lambda: SimpleNamespace(preview="text"), Mock()) is None
    assert pipeline_preview(lambda: object(), Mock()) is None


@pytest.mark.parametrize("previous", [None, "/tmp/kept-state"])
def test_tools_keep_their_state_in_a_throwaway_dir_during_the_preview(
    monkeypatch: pytest.MonkeyPatch, previous: str | None
) -> None:
    if previous is None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_STATE_HOME", previous)
    seen: list[str | None] = []

    def run() -> None:
        seen.append(os.environ.get("XDG_STATE_HOME"))

    pipeline_preview(run, Mock())

    assert seen[0] not in (None, previous)
    assert "apm-preview-state-" in str(seen[0])
    assert not os.path.exists(str(seen[0]))
    assert os.environ.get("XDG_STATE_HOME") == previous


def _install_ctx(**overrides: object) -> SimpleNamespace:
    values = {
        name: f"<{name}>"
        for name in (
            "force",
            "parallel_downloads",
            "scope",
            "auth_resolver",
            "target",
            "runtime",
            "allow_insecure",
            "allow_insecure_hosts",
            "packages",
            "protocol_pref",
            "allow_protocol_fallback",
            "no_policy",
            "audit_override",
            "legacy_skill_paths",
            "trust_transitive_mcp",
            "plan_callback",
            "skill_subset",
            "skill_subset_from_cli",
            "refresh",
            "trust_bin",
            "transaction",
        )
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_pipeline_options_pass_every_install_choice_through() -> None:
    logger, lsp = object(), object()
    outcome = SimpleNamespace(marketplace_provenance="<provenance>")

    options = install_pipeline_options(
        _install_ctx(), outcome, logger=logger, install_mcp=True, lsp_preflight=lsp
    )

    assert options == {
        "post_install_after_services": True,
        "force": "<force>",
        "parallel_downloads": "<parallel_downloads>",
        "logger": logger,
        "scope": "<scope>",
        "auth_resolver": "<auth_resolver>",
        "target": "<target>",
        "allow_insecure": "<allow_insecure>",
        "allow_insecure_hosts": "<allow_insecure_hosts>",
        "marketplace_provenance": "<provenance>",
        "protocol_pref": "<protocol_pref>",
        "allow_protocol_fallback": "<allow_protocol_fallback>",
        "no_policy": "<no_policy>",
        "audit_override": "<audit_override>",
        "legacy_skill_paths": "<legacy_skill_paths>",
        "trust_transitive_mcp": "<trust_transitive_mcp>",
        "install_mcp": True,
        "mcp_runtime": "<runtime>",
        "plan_callback": "<plan_callback>",
        "skill_subset": "<skill_subset>",
        "skill_subset_from_cli": "<skill_subset_from_cli>",
        "refresh": "<refresh>",
        "trust_bin": "<trust_bin>",
        "transaction": "<transaction>",
        "lsp_preflight": lsp,
    }


def test_pipeline_target_falls_back_to_the_runtime() -> None:
    options = install_pipeline_options(
        _install_ctx(target=None), None, logger=None, install_mcp=False, lsp_preflight=None
    )
    assert options["target"] == "<runtime>"
    assert options["install_mcp"] is False


@pytest.mark.parametrize(
    ("packages", "outcome"),
    [((), SimpleNamespace(marketplace_provenance="<p>")), (("acme/x",), None)],
)
def test_marketplace_provenance_needs_requested_packages_and_an_outcome(
    packages: tuple[str, ...], outcome: object
) -> None:
    options = install_pipeline_options(
        _install_ctx(packages=packages), outcome, logger=None, install_mcp=True, lsp_preflight=None
    )
    assert options["marketplace_provenance"] is None
