"""Rewrite eager `from .mod import a, b` re-exports in a package __init__ to PEP 562 lazy getattr."""
import ast, sys
for path in sys.argv[1:]:
    src = open(path).read()
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    mapping, drop = {}, []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            for alias in node.names:
                mapping[alias.asname or alias.name] = (node.module, alias.name)
            drop.append((node.lineno, node.end_lineno))
    keep = []
    for i, line in enumerate(lines, 1):
        if any(a <= i <= b for a, b in drop):
            continue
        keep.append(line)
    body = "".join(keep).rstrip() + "\n"
    entries = "".join(f'    "{k}": ("{m}", "{n}"),\n' for k, (m, n) in mapping.items())
    body += (
        "\n_LAZY = {\n" + entries + "}\n\n\n"
        "def __getattr__(name):\n"
        "    target = _LAZY.get(name)\n"
        "    if target is None:\n"
        '        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")\n'
        "    import importlib\n\n"
        '    value = getattr(importlib.import_module(f".{target[0]}", __name__), target[1])\n'
        "    globals()[name] = value\n"
        "    return value\n\n\n"
        "def __dir__():\n"
        "    return sorted(set(globals()) | set(_LAZY))\n"
    )
    open(path, "w").write(body)
    print(path, len(mapping))
