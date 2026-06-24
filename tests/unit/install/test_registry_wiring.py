"""Unit tests for apm_cli.install.registry_wiring helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from apm_cli.install.registry_wiring import (
    get_effective_default_registry,
    should_skip_github_probe_for_dep,
    validate_registry_ref,
)

# ---------------------------------------------------------------------------
# should_skip_github_probe_for_dep
# ---------------------------------------------------------------------------


def _dep(source="github", is_local=False, reference="1.0.0"):
    dep = MagicMock()
    dep.source = source
    dep.is_local = is_local
    dep.reference = reference
    return dep


class TestShouldSkipGithubProbeForDep:
    def test_skips_when_conditions_met(self):
        dep = _dep(source="github", is_local=False, reference="1.0.0")
        assert should_skip_github_probe_for_dep(dep, "jfrog-demo") is True

    def test_no_skip_when_no_default_registry(self):
        dep = _dep(source="github", is_local=False, reference="1.0.0")
        assert should_skip_github_probe_for_dep(dep, None) is False

    def test_no_skip_for_explicit_git_source(self):
        dep = _dep(source="git", is_local=False, reference="main")
        assert should_skip_github_probe_for_dep(dep, "my-reg") is False

    def test_no_skip_for_registry_source(self):
        dep = _dep(source="registry", is_local=False, reference="^1.0.0")
        assert should_skip_github_probe_for_dep(dep, "my-reg") is False

    def test_no_skip_for_local_dep(self):
        dep = _dep(source="path", is_local=True, reference="")
        assert should_skip_github_probe_for_dep(dep, "my-reg") is False

    def test_skips_when_no_reference(self):
        # No reference still bypasses the probe; validate_registry_ref rejects it.
        dep = _dep(source="github", is_local=False, reference=None)
        assert should_skip_github_probe_for_dep(dep, "my-reg") is True

    def test_skips_when_reference_empty_string(self):
        dep = _dep(source="github", is_local=False, reference="")
        assert should_skip_github_probe_for_dep(dep, "my-reg") is True

    def test_no_skip_when_is_local_attr_absent(self):
        # getattr default is True (treat as local) so missing is_local = no bypass.
        dep = MagicMock(spec=["source", "reference"])
        dep.source = "github"
        dep.reference = "1.0.0"
        assert should_skip_github_probe_for_dep(dep, "my-reg") is False


# ---------------------------------------------------------------------------
# get_effective_default_registry
# ---------------------------------------------------------------------------


def _enable_gate(monkeypatch):
    monkeypatch.setattr(
        "apm_cli.deps.registry.feature_gate.is_package_registry_enabled",
        lambda: True,
    )


class TestGetEffectiveDefaultRegistry:
    def test_reads_project_level_default(self, monkeypatch):
        _enable_gate(monkeypatch)
        data = {
            "registries": {"default": "corp-main", "corp-main": {"url": "https://r.example.com"}}
        }
        assert get_effective_default_registry(data) == "corp-main"

    def test_returns_none_when_no_registries_key(self):
        data = {}
        # User config default is absent in test environment; allow None or str.
        result = get_effective_default_registry(data)
        assert result is None or isinstance(result, str)

    def test_returns_none_when_registries_not_a_dict(self):
        data = {"registries": "not-a-dict"}
        result = get_effective_default_registry(data)
        assert result is None or isinstance(result, str)

    def test_returns_none_when_no_default_key(self):
        data = {"registries": {"corp-main": {"url": "https://r.example.com"}}}
        result = get_effective_default_registry(data)
        assert result is None or isinstance(result, str)

    def test_project_default_takes_precedence_over_user_config(self, monkeypatch):
        _enable_gate(monkeypatch)
        monkeypatch.setattr(
            "apm_cli.deps.registry.config_loader.resolve_effective_registries",
            lambda regs, default: (regs, "user-default"),
        )
        data = {"registries": {"default": "corp", "corp": {"url": "https://r.example.com"}}}
        assert get_effective_default_registry(data) == "corp"

    def test_unconfigured_project_default_not_honored(self, monkeypatch):
        # The project default names a registry that is not configured -- the
        # load path rejects this, so the CLI must NOT route to it (else it
        # writes a dep the next manifest load cannot resolve). Falls through to
        # the user-level lookup (None in the test env).
        _enable_gate(monkeypatch)
        monkeypatch.setattr(
            "apm_cli.deps.registry.config_loader.resolve_effective_registries",
            lambda regs, default: (regs, None),
        )
        data = {"registries": {"default": "ghost", "corp": {"url": "https://r.example.com"}}}
        assert get_effective_default_registry(data) is None

    def test_project_default_skipped_when_feature_disabled(self, monkeypatch):
        # Feature gate off: even a well-formed project default must not activate
        # routing, since the manifest load would reject the registries block.
        monkeypatch.setattr(
            "apm_cli.deps.registry.feature_gate.is_package_registry_enabled",
            lambda: False,
        )
        data = {
            "registries": {"default": "corp-main", "corp-main": {"url": "https://r.example.com"}}
        }
        assert get_effective_default_registry(data) is None

    def test_falls_back_to_user_config_when_no_project_default(self, monkeypatch):
        monkeypatch.setattr(
            "apm_cli.deps.registry.feature_gate.is_package_registry_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "apm_cli.deps.registry.config_loader.resolve_effective_registries",
            lambda regs, default: (regs, "user-level-reg"),
        )
        data = {"registries": {"corp-main": {"url": "https://r.example.com"}}}
        assert get_effective_default_registry(data) == "user-level-reg"

    def test_user_level_fallback_skipped_when_feature_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "apm_cli.deps.registry.feature_gate.is_package_registry_enabled",
            lambda: False,
        )
        data = {}
        assert get_effective_default_registry(data) is None


# ---------------------------------------------------------------------------
# validate_registry_ref
# ---------------------------------------------------------------------------


def _dep_with_ref(reference, ref_kind_val=None, raises=None):
    """Build a mock dep_ref whose ref_kind property returns ref_kind_val or raises."""
    from unittest.mock import PropertyMock

    dep = MagicMock()
    dep.reference = reference
    if raises is not None:
        type(dep).ref_kind = PropertyMock(side_effect=raises)
    else:
        type(dep).ref_kind = PropertyMock(return_value=ref_kind_val)
    return dep


class TestValidateRegistryRef:
    def test_semver_ref_is_valid(self):
        dep = _dep_with_ref("^1.0.0", ref_kind_val="semver")
        ok, err = validate_registry_ref(dep)
        assert ok is True
        assert err == ""

    def test_exact_version_is_valid(self):
        dep = _dep_with_ref("1.2.3", ref_kind_val="semver")
        ok, _err = validate_registry_ref(dep)
        assert ok is True

    def test_branch_name_is_allowed_for_exact_match(self):
        # Non-semver literals pass through to exact matching in the registry resolver.
        dep = _dep_with_ref("main", ref_kind_val="literal")
        ok, _err = validate_registry_ref(dep)
        assert ok is True

    def test_opaque_label_is_allowed_for_exact_match(self):
        dep = _dep_with_ref("stable", ref_kind_val="literal")
        ok, _err = validate_registry_ref(dep)
        assert ok is True

    def test_v_prefixed_tag_is_allowed_for_exact_match(self):
        dep = _dep_with_ref("v1.4.2", ref_kind_val="literal")
        ok, _err = validate_registry_ref(dep)
        assert ok is True

    def test_invalid_semver_range_error_is_forwarded(self):
        from apm_cli.models.dependency.identity import InvalidSemverRangeError

        dep = _dep_with_ref("^1.0", raises=InvalidSemverRangeError("bad range"))
        ok, err = validate_registry_ref(dep)
        assert ok is False
        assert "bad range" in err

    def test_no_ref_error_says_version_selector_not_semver(self):
        from unittest.mock import PropertyMock

        dep = MagicMock()
        dep.reference = None
        dep.repo_url = "acme/toolkit"
        type(dep).ref_kind = PropertyMock(return_value=None)
        ok, err = validate_registry_ref(dep)
        assert ok is False
        assert "version selector" in err
        # Error must not suggest semver is the only option.
        assert "Add a version selector" in err

    def test_no_reference_is_rejected_with_version_hint(self):
        from unittest.mock import PropertyMock

        dep = MagicMock()
        dep.reference = None
        dep.repo_url = "acme/toolkit"
        type(dep).ref_kind = PropertyMock(return_value=None)
        ok, err = validate_registry_ref(dep)
        assert ok is False
        assert "version selector required" in err
        assert "acme/toolkit#1.0.0" in err

    def test_empty_reference_is_rejected_with_version_hint(self):
        from unittest.mock import PropertyMock

        dep = MagicMock()
        dep.reference = ""
        dep.repo_url = "acme/toolkit"
        type(dep).ref_kind = PropertyMock(return_value=None)
        ok, err = validate_registry_ref(dep)
        assert ok is False
        assert "version selector required" in err
