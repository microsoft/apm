from pathlib import Path

p = Path("src/apm_cli/deps/plugin_root_render.py")
s = p.read_text()
old = s[s.index("<<<<<<< HEAD\n"): s.index(">>>>>>> 77f797769a4687c959e4ae865706bdfe59d8ffb0\n") + len(">>>>>>> 77f797769a4687c959e4ae865706bdfe59d8ffb0\n")]
s = s.replace(old, "    from apm_cli.utils.realpath import resolve_path\n\n    root: str | None = None\n")
s = s.replace("                    root = str(package_path.resolve())\n", "                    root = str(resolve_path(package_path))\n")
p.write_text(s)
print("resolved")
