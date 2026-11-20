"""Generator of the lifecycle obligation set and its covering replay rows (#4099).

``python -m tests.lifecycle.generator`` regenerates
``tests/quality/lifecycle_obligations.json`` and
``tests/lifecycle/replay/rows/generated_*.yaml``; ``--check`` fails on drift.
Modules: ``factors`` (vocabulary), ``rules`` (refusal, definition and
equivalence rules with code evidence), ``space`` (the obligation set),
``covering`` (greedy covering rows), ``emit`` (outputs), ``replay`` (the row
runner that credits obligations through ``oracles.check_obligation``).
"""
