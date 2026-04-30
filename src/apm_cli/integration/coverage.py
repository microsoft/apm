"""Primitive coverage validation.

Ensures every primitive registered in ``KNOWN_TARGETS`` has a
corresponding entry in the unified dispatch table.  This check runs
at import time (via test fixtures) to catch wiring omissions that
would otherwise cause silent failures at runtime.
"""

from __future__ import annotations


def check_primitive_coverage(dispatch_table: dict, special_cases: set | None = None) -> None:
    """Assert that every primitive in KNOWN_TARGETS has a handler and vice versa.

    Performs bidirectional validation:
    1. Every primitive in ``KNOWN_TARGETS`` must have a dispatch entry.
    2. Every dispatch entry must map to at least one target (no dead entries).
    3. Every dispatch entry's integrator methods must exist on the class.

    Args:
        dispatch_table: Mapping of primitive name to ``PrimitiveDispatch``
            (from ``dispatch.get_dispatch_table()``).
        special_cases: Primitive names handled outside the table.
            Typically empty when using the unified dispatch table.

    Raises:
        RuntimeError: If any coverage gap is detected.
    """
    from apm_cli.integration.targets import KNOWN_TARGETS

    if special_cases is None:
        special_cases = set()

    all_primitives: set[str] = set()
    for target in KNOWN_TARGETS.values():
        all_primitives.update(target.primitives.keys())

    handled = set(dispatch_table.keys()) | special_cases
    missing = all_primitives - handled
    if missing:
        raise RuntimeError(
            f"Primitives {sorted(missing)} are registered in KNOWN_TARGETS "
            f"but have no integrator in the dispatch table. "
            f"Add entries to the dispatch table or to the special_cases set."
        )

    # Reverse check: no dead entries in dispatch table
    extra = set(dispatch_table.keys()) - all_primitives - special_cases
    if extra:
        raise RuntimeError(
            f"Dispatch table has entries {sorted(extra)} not present in "
            f"any KNOWN_TARGETS profile. Remove stale entries."
        )

    # Method existence check
    for name, entry in dispatch_table.items():
        if not hasattr(entry, "integrator_class"):
            continue  # plain dict values (test mode)
        cls = entry.integrator_class
        for method_attr in ("integrate_method", "sync_method"):
            method_name = getattr(entry, method_attr, None)
            if method_name and not hasattr(cls, method_name):
                raise RuntimeError(
                    f"{cls.__name__} missing method '{method_name}' "
                    f"(referenced by dispatch entry '{name}.{method_attr}')"
                )
