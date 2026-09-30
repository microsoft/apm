"""Static guard for lock-specific install freshness policy."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
OWNER = "src/apm_cli/deps/tiered_ref_resolver.py"
RULE = "transport-platform-ref-freshness"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("return cls.LOCKED_OR_CURRENT", "return cls.REPRODUCIBLE"),
        ("dep_ref.get_unique_key(),", '"",'),
        (
            "        except (ValueError, RuntimeError) as exc:\n",
            "        except TypeError as exc:\n",
        ),
        (
            "            return False\n        with self._coalesce_lock:\n",
            "            raise\n        with self._coalesce_lock:\n",
        ),
        (
            "            return False\n        with self._coalesce_lock:\n",
            "            return True\n        with self._coalesce_lock:\n",
        ),
        (
            "        except (ValueError, RuntimeError) as exc:\n",
            "        except Exception as exc:\n",
        ),
    ],
)
def test_install_freshness_guard_rejects_blanket_bare_cache_replay(old: str, new: str) -> None:
    """An unrelated lock must not authorize a stale mutable-ref cache answer."""
    baseline = run_selected_rules(ROOT, (RULE,))
    assert baseline.failures == ()
    assert baseline.violations == ()
    source = (ROOT / OWNER).read_text(encoding="utf-8")
    mutated = source.replace(old, new, 1)
    assert mutated != source
    report = run_selected_rules(ROOT, (RULE,), source_overrides={OWNER: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == RULE for violation in report.violations)
