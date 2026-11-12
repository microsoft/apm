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
    ("path", "content"),
    [
        ("pkg/AGENTS.md", b"Package instructions\n"),
        ("pkg/chain.md", b"Package instructions\n"),
        ("pkg/notes.crlf", b"line one\r\nline two\r\n"),
    ],
)
def test_sparse_file_fetch_returns_link_target_content(
    tmp_path: Path, path: str, content: bytes
) -> None:
    with _transport(tmp_path) as transport:
        assert transport.fetch_file(path) == content


def test_sparse_file_fetch_rejects_escaping_link(tmp_path: Path) -> None:
    with (
        _transport(tmp_path, {"pkg/escape.md": "../../outside.md"}) as transport,
        pytest.raises(GitFileTransportError, match="resolves outside the repository"),
    ):
        transport.fetch_file("pkg/escape.md")
