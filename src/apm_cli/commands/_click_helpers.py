from __future__ import annotations

import click


def _collect_ctx_params(ctx: click.Context) -> dict[str, object]:
    return dict(ctx.params)
