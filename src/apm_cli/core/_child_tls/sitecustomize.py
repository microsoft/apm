# APM child-runtime TLS trust shim.
#
# Python auto-imports ``sitecustomize`` at interpreter startup from any
# directory on sys.path / PYTHONPATH. apm_cli.core.tls_trust.build_child_tls_env
# prepends this directory to a child runtime's PYTHONPATH so the child re-runs
# the OS-trust bootstrap in its own process (the parent cannot monkeypatch a
# child's ssl module across exec()).
#
# Must stay SILENT (write nothing to stdout/stderr) and never raise -- a broken
# bootstrap must not disturb the child runtime's own output or startup.
try:
    from apm_cli.core.tls_trust import configure_tls_trust

    configure_tls_trust()
except Exception:
    pass
