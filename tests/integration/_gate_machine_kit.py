"""Compatibility alias: the GATE machine kit moved to ``tests.lifecycle.machines._gate_kit``.

Importing this module yields the moved module itself, so ``KNOWN_CELLS``,
``VISITED_CELLS`` and every helper stay one object under both names.
"""

import sys

from tests.lifecycle.machines import _gate_kit

sys.modules[__name__] = _gate_kit
