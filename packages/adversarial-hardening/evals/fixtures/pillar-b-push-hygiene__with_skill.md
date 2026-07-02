# Harden the lifecycle-scripts runner

## TL;DR

A cross-module fix to the runner. The first fold tried to ship an
uncollected test plus a stray fixture; the push-hygiene gate REJECTED
it, we looped back, and the final diff is minimal and fully wired.

## Implementation

The runner fix touches two modules, so it carries an integration test,
not only a unit test (right-altitude). The push-hygiene gate runs
pytest --collect-only against the merge-queue lane (CI-COLLECTED) after
every fold.

The first fold added tests/red_team/test_uncollected.py (not collected
by the lane) and tests/orphan_fixture.bin (stray). The hygiene gate
returned REJECT with reasons uncollected-test and orphan-artifact, so
the fold did not advance. After the loop-back the diff is diff-minimal:
no orphan fixtures, and the new tests are collected by the lane.

## How to test

uv run pytest tests/integration/test_runner_crossmodule.py
