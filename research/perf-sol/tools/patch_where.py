from pathlib import Path

p = Path("tests/benchmarks/cpu_attr.py")
s = p.read_text()

old = '''    def counted(path, *args, **kwargs):
        frame, owner, depth = sys._getframe(1), "<no apm frame>", 0
        while frame is not None and depth < 60:
            where = _where(frame.f_code)
            if where.startswith("apm_cli/"):
                owner = f"{where}:{frame.f_code.co_name}"
                break
            frame, depth = frame.f_back, depth + 1
        _realpath_calls[owner] += 1
        return original(path, *args, **kwargs)'''
new = '''    cwd = os.getcwd().replace("\\\\", "/").lower().rstrip("/") + "/"
    cache = (os.environ.get("APM_CACHE_DIR") or "").replace("\\\\", "/").lower().rstrip("/")
    home = (os.environ.get("APM_HOME") or "").replace("\\\\", "/").lower().rstrip("/")
    temp = __import__("tempfile").gettempdir().replace("\\\\", "/").lower().rstrip("/")

    def where_class(path) -> str:
        try:
            text = os.fspath(path)
        except TypeError:
            return "?"
        if isinstance(text, bytes):
            text = text.decode(errors="replace")
        text = os.path.abspath(text).replace("\\\\", "/").lower()
        if ".inc." in text or "/.staging" in text or ".tmp" in text:
            kind = "staging"
        elif cache and text.startswith(cache + "/"):
            kind = "cache"
        elif home and text.startswith(home + "/"):
            kind = "apm_home"
        elif text.startswith(cwd):
            kind = "project/apm_modules" if "/apm_modules/" in text + "/" else "project/deploy"
        elif temp and text.startswith(temp + "/"):
            kind = "temp"
        else:
            kind = "other"
        return kind

    def counted(path, *args, **kwargs):
        frame, owner, depth = sys._getframe(1), "<no apm frame>", 0
        while frame is not None and depth < 60:
            where = _where(frame.f_code)
            if where.startswith("apm_cli/") and not where.startswith("apm_cli/utils/realpath.py"):
                owner = f"{where}:{frame.f_code.co_name}"
                break
            frame, depth = frame.f_back, depth + 1
        _realpath_calls[owner] += 1
        _realpath_where[f"{where_class(path)} <- {owner}"] += 1
        return original(path, *args, **kwargs)'''
assert old in s
s = s.replace(old, new, 1)
s = s.replace("_realpath_calls = collections.Counter()\n", "_realpath_calls = collections.Counter()\n_realpath_where = collections.Counter()\n", 1)
old = '''        "realpath_calls_total": sum(_realpath_calls.values()),'''
new = '''        "realpath_calls_total": sum(_realpath_calls.values()),
        "realpath_calls_by_where_owner": top(_realpath_where, 120),'''
assert old in s
s = s.replace(old, new, 1)
p.write_text(s)
print("patched")
