# Lifecycle-scripts runner hardening

## Summary

Hardened the runner and added a large suite of adversarial tests under
tests/red_team/ to cover every case we could think of.

## Changes

- New red_team/ tests added (about 25k lines across the sweep).
- Added tests/orphan_fixture.bin used by some of the scratch tests.
- The uncollected test ships alongside the fix.

The merge-queue lane does not collect tests/red_team/ but the coverage
is there for future reference.
