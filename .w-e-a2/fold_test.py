from pathlib import Path
p = Path("tests/integration/test_marketplace_object_form_dry_run_lifecycle.py")
s = p.read_text()
old = '''    run(online, "marketplace", "add", _REMOTE_A, "--name", _ALIAS, "--ref", "main")
    run(online, "install", *scope, *_INSTALL_FLAGS)
'''
new = '''    run(online, "marketplace", "add", _REMOTE_A, "--name", _ALIAS, "--ref", "main")
    # Before any lock exists the read-only preview cannot resolve the
    # declaration: it reports that instead of crashing, and writes nothing.
    before = _snapshot(root)
    unlocked = _output(run(online, "install", *scope, "--dry-run", *_INSTALL_FLAGS))
    assert "Dependency preview incomplete" in unlocked
    assert "Cannot compute install path" not in unlocked
    assert _snapshot(root) == before
    run(online, "install", *scope, *_INSTALL_FLAGS)
'''
assert old in s
s = s.replace(old, new)
old = '''        assert _PLUGIN in output
        assert "Cannot compute install path" not in output
        assert _snapshot(root) == before
'''
new = '''        assert _PLUGIN in output
        assert "APM dependencies (1):" in output
        assert "Cannot compute install path" not in output
        assert "Dependency preview incomplete" not in output
        assert _snapshot(root) == before

    # --update skips lock replay; the preview still lists the declaration once.
    before = _snapshot(root)
    refresh = _output(run(env, "install", *scope, "--update", "--dry-run", *_INSTALL_FLAGS))
    assert "APM dependencies (1):" in refresh
    assert "Cannot compute install path" not in refresh
    assert _snapshot(root) == before
'''
assert old in s
s = s.replace(old, new)
p.write_text(s)
