"""Child-process TLS trust bootstrap package.

Holds the two artifacts copied into a child runtime venv's site-packages by
``ensure_child_tls_bootstrap`` in ``apm_cli.core.tls_trust``:

* ``_apm_tls_bootstrap.py`` -- a self-contained OS-trust bootstrap with NO
  ``apm_cli`` dependency (it only needs ``truststore``).
* ``_apm_tls.pth`` -- a one-line path-config file (``import _apm_tls_bootstrap``)
  that Python executes at interpreter startup, so trust is delivered at
  venv-setup time instead of by mutating the child's ``PYTHONPATH`` at spawn
  time (which would shadow a user/corporate ``sitecustomize.py``).

The ``__init__`` exists only so the directory ships as package data; the
bootstrap files are copied by path, not imported as submodules of this package.
"""
