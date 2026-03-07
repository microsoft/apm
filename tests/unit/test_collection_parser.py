"""Unit tests for apm_cli.deps.collection_parser."""

import pytest
import yaml

from apm_cli.deps.collection_parser import (
    CollectionItem,
    CollectionManifest,
    parse_collection_yml,
)


class TestCollectionItem:
    """Tests for CollectionItem dataclass."""

    def test_subdirectory_prompt(self):
        item = CollectionItem(path="p.prompt.md", kind="prompt")
        assert item.subdirectory == "prompts"

    def test_subdirectory_instruction(self):
        item = CollectionItem(path="i.instructions.md", kind="instruction")
        assert item.subdirectory == "instructions"

    def test_subdirectory_chat_mode_hyphenated(self):
        item = CollectionItem(path="c.chatmode.md", kind="chat-mode")
        assert item.subdirectory == "chatmodes"

    def test_subdirectory_chatmode_no_hyphen(self):
        item = CollectionItem(path="c.chatmode.md", kind="chatmode")
        assert item.subdirectory == "chatmodes"

    def test_subdirectory_agent(self):
        item = CollectionItem(path="a.agent.md", kind="agent")
        assert item.subdirectory == "agents"

    def test_subdirectory_context(self):
        item = CollectionItem(path="c.context.md", kind="context")
        assert item.subdirectory == "contexts"

    def test_subdirectory_unknown_defaults_to_prompts(self):
        item = CollectionItem(path="x.md", kind="unknown_kind")
        assert item.subdirectory == "prompts"

    def test_subdirectory_case_insensitive(self):
        item = CollectionItem(path="p.md", kind="PROMPT")
        assert item.subdirectory == "prompts"

    def test_subdirectory_instruction_uppercase(self):
        item = CollectionItem(path="i.md", kind="INSTRUCTION")
        assert item.subdirectory == "instructions"


class TestCollectionManifest:
    """Tests for CollectionManifest dataclass."""

    def _make_manifest(self, items=None):
        if items is None:
            items = [CollectionItem(path="p.md", kind="prompt")]
        return CollectionManifest(
            id="test-id",
            name="Test",
            description="Desc",
            items=items,
        )

    def test_item_count_single(self):
        manifest = self._make_manifest()
        assert manifest.item_count == 1

    def test_item_count_multiple(self):
        items = [
            CollectionItem(path="a.md", kind="prompt"),
            CollectionItem(path="b.md", kind="instruction"),
            CollectionItem(path="c.md", kind="agent"),
        ]
        manifest = self._make_manifest(items)
        assert manifest.item_count == 3

    def test_item_count_empty(self):
        manifest = CollectionManifest(id="x", name="X", description="X", items=[])
        assert manifest.item_count == 0

    def test_get_items_by_kind_match(self):
        items = [
            CollectionItem(path="a.md", kind="prompt"),
            CollectionItem(path="b.md", kind="instruction"),
            CollectionItem(path="c.md", kind="prompt"),
        ]
        manifest = self._make_manifest(items)
        result = manifest.get_items_by_kind("prompt")
        assert len(result) == 2
        assert all(i.kind == "prompt" for i in result)

    def test_get_items_by_kind_no_match(self):
        manifest = self._make_manifest()
        result = manifest.get_items_by_kind("agent")
        assert result == []

    def test_get_items_by_kind_case_insensitive(self):
        items = [CollectionItem(path="p.md", kind="Prompt")]
        manifest = self._make_manifest(items)
        result = manifest.get_items_by_kind("PROMPT")
        assert len(result) == 1

    def test_optional_tags_none_by_default(self):
        manifest = self._make_manifest()
        assert manifest.tags is None

    def test_optional_display_none_by_default(self):
        manifest = self._make_manifest()
        assert manifest.display is None


class TestParseCollectionYml:
    """Tests for parse_collection_yml function."""

    VALID_YAML = b"""
id: my-collection
name: My Collection
description: A wonderful collection
items:
  - path: prompts/hello.prompt.md
    kind: prompt
  - path: instructions/setup.instructions.md
    kind: instruction
tags:
  - ai
  - tools
display:
  ordering: alpha
"""

    def test_parse_valid_full_manifest(self):
        manifest = parse_collection_yml(self.VALID_YAML)
        assert manifest.id == "my-collection"
        assert manifest.name == "My Collection"
        assert manifest.description == "A wonderful collection"
        assert manifest.tags == ["ai", "tools"]
        assert manifest.display == {"ordering": "alpha"}

    def test_parse_valid_items(self):
        manifest = parse_collection_yml(self.VALID_YAML)
        assert len(manifest.items) == 2
        assert manifest.items[0].path == "prompts/hello.prompt.md"
        assert manifest.items[0].kind == "prompt"
        assert manifest.items[1].path == "instructions/setup.instructions.md"
        assert manifest.items[1].kind == "instruction"

    def test_parse_minimal_manifest_no_optional_fields(self):
        content = b"""
id: minimal
name: Minimal
description: Minimal collection
items:
  - path: p.md
    kind: prompt
"""
        manifest = parse_collection_yml(content)
        assert manifest.id == "minimal"
        assert manifest.tags is None
        assert manifest.display is None
        assert manifest.item_count == 1

    def test_missing_id_raises_value_error(self):
        content = b"""
name: Test
description: Desc
items:
  - path: p.md
    kind: prompt
"""
        with pytest.raises(ValueError, match="missing required fields"):
            parse_collection_yml(content)

    def test_missing_name_raises_value_error(self):
        content = b"""
id: test
description: Desc
items:
  - path: p.md
    kind: prompt
"""
        with pytest.raises(ValueError, match="missing required fields"):
            parse_collection_yml(content)

    def test_missing_description_raises_value_error(self):
        content = b"""
id: test
name: Test
items:
  - path: p.md
    kind: prompt
"""
        with pytest.raises(ValueError, match="missing required fields"):
            parse_collection_yml(content)

    def test_missing_items_raises_value_error(self):
        content = b"""
id: test
name: Test
description: Desc
"""
        with pytest.raises(ValueError, match="missing required fields"):
            parse_collection_yml(content)

    def test_multiple_missing_fields_error_lists_all(self):
        content = b"""
name: Test
"""
        with pytest.raises(ValueError, match="missing required fields"):
            parse_collection_yml(content)

    def test_empty_items_list_raises_value_error(self):
        content = b"""
id: test
name: Test
description: Desc
items: []
"""
        with pytest.raises(ValueError, match="at least one item"):
            parse_collection_yml(content)

    def test_items_not_a_list_raises_value_error(self):
        content = b"""
id: test
name: Test
description: Desc
items: not-a-list
"""
        with pytest.raises(ValueError, match="'items' must be a list"):
            parse_collection_yml(content)

    def test_item_not_a_dict_raises_value_error(self):
        content = b"""
id: test
name: Test
description: Desc
items:
  - just-a-string
"""
        with pytest.raises(ValueError, match="item 0 must be a dictionary"):
            parse_collection_yml(content)

    def test_item_missing_path_raises_value_error(self):
        content = b"""
id: test
name: Test
description: Desc
items:
  - kind: prompt
"""
        with pytest.raises(ValueError, match="item 0 missing required field 'path'"):
            parse_collection_yml(content)

    def test_item_missing_kind_raises_value_error(self):
        content = b"""
id: test
name: Test
description: Desc
items:
  - path: p.md
"""
        with pytest.raises(ValueError, match="item 0 missing required field 'kind'"):
            parse_collection_yml(content)

    def test_invalid_yaml_raises_value_error(self):
        content = b"key: : invalid: yaml: content:"
        with pytest.raises(ValueError, match="Invalid YAML format"):
            parse_collection_yml(content)

    def test_non_dict_yaml_raises_value_error(self):
        content = b"- just\n- a\n- list"
        with pytest.raises(ValueError, match="must be a dictionary"):
            parse_collection_yml(content)

    def test_null_yaml_raises_value_error(self):
        content = b"null"
        with pytest.raises(ValueError, match="must be a dictionary"):
            parse_collection_yml(content)

    def test_second_item_missing_kind_raises_value_error(self):
        """Verify error message includes correct item index."""
        content = b"""
id: test
name: Test
description: Desc
items:
  - path: p0.md
    kind: prompt
  - path: p1.md
"""
        with pytest.raises(ValueError, match="item 1 missing required field 'kind'"):
            parse_collection_yml(content)

    def test_all_kind_subdirectories(self):
        """Verify all supported kind mappings via parse."""
        kinds = [
            ("prompt", "prompts"),
            ("instruction", "instructions"),
            ("chat-mode", "chatmodes"),
            ("chatmode", "chatmodes"),
            ("agent", "agents"),
            ("context", "contexts"),
        ]
        for kind, expected_subdir in kinds:
            content = f"""
id: test
name: Test
description: Desc
items:
  - path: file.md
    kind: {kind}
""".encode()
            manifest = parse_collection_yml(content)
            assert (
                manifest.items[0].subdirectory == expected_subdir
            ), f"kind={kind!r} should map to {expected_subdir!r}"

    def test_get_items_by_kind_after_parse(self):
        manifest = parse_collection_yml(self.VALID_YAML)
        prompts = manifest.get_items_by_kind("prompt")
        assert len(prompts) == 1
        assert prompts[0].path == "prompts/hello.prompt.md"
