"""Tests for ``policy.dependencies.require_pinned_constraint`` schema + parser."""

from __future__ import annotations

import pytest

from apm_cli.policy.inheritance import merge_policies
from apm_cli.policy.parser import PolicyValidationError, load_policy
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy


class TestSchemaDefaults:
    def test_dependency_policy_default_field_value_is_false(self):
        assert DependencyPolicy().require_pinned_constraint is False

    def test_apm_policy_default_field_value_is_false(self):
        assert ApmPolicy().dependencies.require_pinned_constraint is False


class TestParser:
    def test_dependency_policy_field_parses_from_yaml(self):
        policy, _ = load_policy("dependencies:\n  require_pinned_constraint: true\n")
        assert policy.dependencies.require_pinned_constraint is True

    def test_dependency_policy_field_parses_false_explicitly(self):
        policy, _ = load_policy("dependencies:\n  require_pinned_constraint: false\n")
        assert policy.dependencies.require_pinned_constraint is False

    def test_dependency_policy_field_parses_from_yaml_omitted_defaults_false(self):
        policy, _ = load_policy("dependencies: {}\n")
        assert policy.dependencies.require_pinned_constraint is False

    def test_non_bool_value_raises_validation_error(self):
        with pytest.raises(PolicyValidationError) as exc:
            load_policy("dependencies:\n  require_pinned_constraint: 'yes'\n")
        assert "require_pinned_constraint" in str(exc.value)

    def test_field_independent_of_other_dependency_settings(self):
        policy, _ = load_policy(
            "dependencies:\n  allow:\n    - acme-org/*\n  require_pinned_constraint: true\n"
        )
        assert policy.dependencies.allow == ("acme-org/*",)
        assert policy.dependencies.require_pinned_constraint is True


class TestInheritanceMerge:
    def test_dependency_policy_field_round_trips_through_inheritance_merge(self):
        parent = ApmPolicy(dependencies=DependencyPolicy(require_pinned_constraint=True))
        child = ApmPolicy()  # default False
        merged = merge_policies(parent, child)
        # Parent's strict requirement wins (strict-wins semantics).
        assert merged.dependencies.require_pinned_constraint is True

    def test_child_can_enable_when_parent_disabled(self):
        parent = ApmPolicy()
        child = ApmPolicy(dependencies=DependencyPolicy(require_pinned_constraint=True))
        merged = merge_policies(parent, child)
        assert merged.dependencies.require_pinned_constraint is True

    def test_both_disabled_stays_disabled(self):
        parent = ApmPolicy()
        child = ApmPolicy()
        merged = merge_policies(parent, child)
        assert merged.dependencies.require_pinned_constraint is False

    def test_child_cannot_relax_parent(self):
        """Strict-wins: child False cannot override parent True."""
        parent = ApmPolicy(dependencies=DependencyPolicy(require_pinned_constraint=True))
        child = ApmPolicy(dependencies=DependencyPolicy(require_pinned_constraint=False))
        merged = merge_policies(parent, child)
        assert merged.dependencies.require_pinned_constraint is True
