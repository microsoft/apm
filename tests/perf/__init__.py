"""Opt-in performance scenarios for the install pipeline.

This package is intentionally NOT registered in CI. To run, set
``PYTEST_PERF=1`` and invoke pytest against ``tests/perf``::

    PYTEST_PERF=1 pytest tests/perf -v -s

Why opt-in:

- Scenarios clone real-world giant repositories (Kubernetes, the
  TypeScript compiler) into ``/tmp/perf-atlas-clones/``. The clones
  total several GB and take minutes the first time. CI runners are
  ephemeral and would re-clone every job.
- Wall-time numbers are sensitive to disk pressure, CPU steal time,
  and concurrent workloads. CI rarely satisfies the "quiet machine"
  requirement that meaningful perf numbers need.
- The harness is a tool for engineers triaging install slowness,
  not a regression detector. The benchmarks/ suite already guards
  algorithmic regressions cheaply (file-count scaling tests).

If you want gentle in-CI scaling guards, use ``tests/benchmarks/``.
"""
