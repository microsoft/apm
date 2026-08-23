"""Unit tests for load_frontmatter handling of Markdown horizontal rules."""

from apm_cli.utils.yaml_io import load_frontmatter


def test_load_frontmatter_with_valid_line1_header(tmp_path):
    md_content = """---
name: sample-skill
description: A sample skill
---
# Main Content

This is body content.
"""
    file_path = tmp_path / "sample.skill.md"
    file_path.write_text(md_content, encoding="utf-8")

    post = load_frontmatter(file_path)
    assert post.metadata.get("name") == "sample-skill"
    assert post.metadata.get("description") == "A sample skill"
    assert "# Main Content" in post.content


def test_load_frontmatter_with_middle_horizontal_rules(tmp_path):
    md_content = """# Dataverse Guide

Overview text...

---

## 1. Code Examples

```python
def foo():
    pass
```

---

## 2. Next Steps
"""
    file_path = tmp_path / "guide.instructions.md"
    file_path.write_text(md_content, encoding="utf-8")

    # Before fix, middle horizontal rules caused ScannerError
    post = load_frontmatter(file_path)
    assert post.metadata == {}
    assert "# Dataverse Guide" in post.content
    assert "def foo():" in post.content
    assert "## 2. Next Steps" in post.content


def test_load_frontmatter_with_supported_four_hyphen_fence(tmp_path):
    md_content = """----
name: sample-skill
----
# Main Content
"""
    file_path = tmp_path / "sample.skill.md"
    file_path.write_text(md_content, encoding="utf-8")

    post = load_frontmatter(file_path)
    assert post.metadata == {"name": "sample-skill"}
    assert "# Main Content" in post.content


def test_load_frontmatter_with_indented_horizontal_rule_on_line1(tmp_path):
    md_content = """  ---
# Guide

---

interval: daily

---

Body content.
"""
    file_path = tmp_path / "guide.instructions.md"
    file_path.write_text(md_content, encoding="utf-8")

    post = load_frontmatter(file_path)
    assert post.metadata == {}
    assert post.content == md_content
