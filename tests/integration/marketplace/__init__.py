# Integration tests for apm marketplace commands.
#
# Test tiers:
#   unit        -- tests/unit/marketplace/   (mocked, fast)
#   integration -- this directory            (real disk, mocked network)
#   live e2e    -- test_live_e2e.py          (env-var-gated, real network)
