"""Child-process TLS trust shim package.

Holds ``sitecustomize.py``, which Python auto-imports at interpreter startup
from any directory on ``sys.path`` / ``PYTHONPATH``. ``build_child_tls_env``
in ``apm_cli.core.tls_trust`` prepends this directory to a child's
``PYTHONPATH`` so each Python runtime re-runs the OS-trust bootstrap in its
own process. The ``__init__`` exists only so the directory ships as package
data; the shim itself is imported by path, not as a submodule.
"""
