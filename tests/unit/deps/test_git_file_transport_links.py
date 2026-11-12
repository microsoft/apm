"""Real-Git regression: ``path:`` file fetches return link content (#3457).

The sparse file transport inherits the ``core.symlinks=false`` pin, so a
tracked link would otherwise come back as its target-path text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps.git_file_transport import GitFileTransportError, GitSparseFileTransport
from apm_cli.models.apm_package import DependencyReference
from tests.utils.git_link_fixture import bare_clone, build_link_origin, neutral_git_env

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]


def _transport(tmp_path: Path, extra_links: dict[str, str] | None = None) -> GitSparseFileTransport:
    origin, _ = build_link_origin(tmp_path, extra_links)
    url = bare_clone(origin, tmp_path / "origin.git").resolve().as_uri()
    return GitSparseFileTransport(
        DependencyReference.parse("github.com/acme/linked-pkg"),
        "main",
        build_repo_url_fn=lambda *args, **kwargs: url,
        git_env=neutral_git_env(),
    )


@pytest.mark.parametrize(
    ("link", "target"),
    [
        ("pkg/AGENTS.md", "pkg/CLAUDE.md"),
        ("pkg/chain.md", "pkg/CLAUDE.md"),
        ("pkg/notes.crlf", "shared/notes.crlf"),
    ],
)
def test_sparse_file_fetch_returns_link_target_content(
    tmp_path: Path, link: str, target: str
) -> None:
    """A link fetch returns exactly what fetching its target returns."""
    with _transport(tmp_path) as transport:
        content = transport.fetch_file(link)
        assert content == transport.fetch_file(target)
    assert content.replace(b"\r\n", b"\n") in {b"Package instructions\n", b"line one\nline two\n"}


def test_sparse_file_fetch_rejects_escaping_link(tmp_path: Path) -> None:
    with (
        _transport(tmp_path, {"pkg/escape.md": "../../outside.md"}) as transport,
        pytest.raises(GitFileTransportError, match="resolves outside the repository"),
    ):
        transport.fetch_file("pkg/escape.md")
