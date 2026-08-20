"""Provenance generator for the static YAML-bomb fixtures.

A classic billion-laughs nest: each level is a list of WIDTH aliases to the
prior level's anchor. If a loader EXPANDED aliases, level N would hold
WIDTH**N nodes (9**12 ~ 2.8e11); PyYAML keeps shared references, so the
in-memory object is a small DAG. The fixtures let the suite prove that.
"""

from pathlib import Path

HERE = Path(__file__).parent / "apm_yml"


def bomb(levels: int, width: int, key_prefix: str) -> tuple[str, str]:
    lines = [f'  {key_prefix}0: &b0 "lol"']
    prev = "b0"
    for i in range(1, levels + 1):
        refs = ", ".join([f"*{prev}"] * width)
        lines.append(f"  {key_prefix}{i}: &b{i} [{refs}]")
        prev = f"b{i}"
    return "\n".join(lines), prev


body, _ = bomb(12, 9, "lvl")
(HERE / "bomb_unknown_events.yml").write_text("lifecycle:\n" + body + "\n", encoding="utf-8")

under, top = bomb(12, 9, "n")
(HERE / "bomb_under_event.yml").write_text(
    "lifecycle:\n" + under + f"\n  post-install: *{top}\n", encoding="utf-8"
)
print("generated; top alias =", top)
