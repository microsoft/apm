""".goosehints formatter for Goose (Block) integration.

Generates a lightweight ``.goosehints`` stub at the project root that imports
AGENTS.md via Goose's ``@path`` preprocessor (Goose resolves ``@./AGENTS.md``
at load time, up to an import depth of 3).  The instruction roll-up itself is
produced by the AGENTS.md pipeline, so the stub is just the thin import
wrapper -- identical mechanics to the GEMINI.md stub, hence the reuse of
:class:`GeminiFormatter`'s logic via overridable class attributes.

Ref: https://goose-docs.ai/docs/guides/context-engineering/using-goosehints/
"""

from .gemini_formatter import GeminiFormatter


class GooseFormatter(GeminiFormatter):
    """Formatter for the ``.goosehints`` import stub.

    Reuses :class:`GeminiFormatter` wholesale and only swaps the output
    filename and title; the ``@./AGENTS.md`` import line is inherited.
    """

    _stub_filename = ".goosehints"
    _stub_title = "# Goose hints"
