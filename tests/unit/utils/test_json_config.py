"""The MCP JSON config writer keeps the user's file style (#4526).

Every oracle is hand-written bytes; none is computed through the owner.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.utils.json_config import write_json_config

_APM = {"command": "apm-srv"}


def _add_apm_server(path: Path) -> dict:
    data = json.loads(path.read_bytes().decode("utf-8"))
    data["mcpServers"]["apm"] = dict(_APM)
    return data


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        pytest.param(
            b'{\n    "mcpServers": {\n        "mine": {\n            "command": "x"\n'
            b"        }\n    }\n}\n",
            b'{\n    "mcpServers": {\n        "mine": {\n            "command": "x"\n'
            b'        },\n        "apm": {\n            "command": "apm-srv"\n        }\n'
            b"    }\n}\n",
            id="four-spaces-final-newline",
        ),
        pytest.param(
            b'{\n\t"mcpServers": {\n\t\t"mine": {\n\t\t\t"command": "x"\n\t\t}\n\t}\n}',
            b'{\n\t"mcpServers": {\n\t\t"mine": {\n\t\t\t"command": "x"\n\t\t},\n'
            b'\t\t"apm": {\n\t\t\t"command": "apm-srv"\n\t\t}\n\t}\n}',
            id="tab-no-final-newline",
        ),
        pytest.param(
            b'{\r\n  "mcpServers": {\r\n    "mine": {\r\n      "command": "x"\r\n'
            b"    }\r\n  }\r\n}\r\n",
            b'{\r\n  "mcpServers": {\r\n    "mine": {\r\n      "command": "x"\r\n'
            b'    },\r\n    "apm": {\r\n      "command": "apm-srv"\r\n    }\r\n  }\r\n}\r\n',
            id="crlf",
        ),
        pytest.param(
            '{\n  "mcpServers": {\n    "mine": {\n      "command": "caf\u00e9"\n'
            "    }\n  }\n}\n".encode(),
            '{\n  "mcpServers": {\n    "mine": {\n      "command": "caf\u00e9"\n'
            '    },\n    "apm": {\n      "command": "apm-srv"\n    }\n  }\n}\n'.encode(),
            id="literal-non-ascii",
        ),
    ],
)
def test_an_added_entry_takes_the_file_style_and_user_bytes_stay(
    tmp_path: Path, original: bytes, expected: bytes
) -> None:
    path = tmp_path / "mcp.json"
    path.write_bytes(original)

    assert write_json_config(path, _add_apm_server(path), default_trailing_newline=False)

    assert path.read_bytes() == expected
    data = json.loads(path.read_bytes().decode("utf-8"))
    del data["mcpServers"]["apm"]
    assert write_json_config(path, data, default_trailing_newline=False)
    assert path.read_bytes() == original


def test_an_existing_entry_is_updated_in_place(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_bytes(
        b'{\n    "mcpServers": {\n        "apm": {\n            "command": "old"\n        },\n'
        b'        "mine": {\n            "args": [\n                "-y"\n            ]\n'
        b"        }\n    }\n}\n"
    )
    data = json.loads(path.read_bytes())
    data["mcpServers"]["apm"]["command"] = "new"
    data["mcpServers"]["second"] = {"command": "s"}

    assert write_json_config(path, data, default_trailing_newline=False)

    assert path.read_bytes() == (
        b'{\n    "mcpServers": {\n        "apm": {\n            "command": "new"\n        },\n'
        b'        "mine": {\n            "args": [\n                "-y"\n            ]\n'
        b'        },\n        "second": {\n            "command": "s"\n        }\n    }\n}\n'
    )


def test_escaped_non_ascii_stays_escaped(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    original = b'{\n  "mcpServers": {\n    "mine": {\n      "command": "caf\\u00e9"\n    }\n  }\n}'
    path.write_bytes(original)

    data = json.loads(original)
    assert not write_json_config(path, data, default_trailing_newline=True)
    assert path.read_bytes() == original


def test_unchanged_bytes_skip_the_write(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    original = b'{\n    "mcpServers": {}\n}\n'
    path.write_bytes(original)
    before = path.stat().st_mtime_ns
    path.chmod(0o444)

    try:
        assert not write_json_config(path, {"mcpServers": {}}, default_trailing_newline=False)
    finally:
        path.chmod(0o644)
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == before


@pytest.mark.parametrize(
    ("trailing_newline", "expected"),
    [
        (False, b'{\n  "mcpServers": {\n    "apm": {\n      "command": "apm-srv"\n    }\n  }\n}'),
        (True, b'{\n  "mcpServers": {\n    "apm": {\n      "command": "apm-srv"\n    }\n  }\n}\n'),
    ],
)
def test_a_new_file_uses_the_default_style(
    tmp_path: Path, trailing_newline: bool, expected: bytes
) -> None:
    path = tmp_path / "mcp.json"

    assert write_json_config(
        path, {"mcpServers": {"apm": dict(_APM)}}, default_trailing_newline=trailing_newline
    )

    assert path.read_bytes() == expected


@pytest.mark.parametrize("original", [b'{"mcpServers": {}}', b'{"mcpServers": {}}\r\n', b"  \n"])
def test_a_minified_or_blank_file_uses_the_default_style(tmp_path: Path, original: bytes) -> None:
    path = tmp_path / "mcp.json"
    path.write_bytes(original)

    assert write_json_config(
        path, {"mcpServers": {"apm": dict(_APM)}}, default_trailing_newline=True
    )

    assert path.read_bytes() == (
        b'{\n  "mcpServers": {\n    "apm": {\n      "command": "apm-srv"\n    }\n  }\n}\n'
    )


def test_a_new_file_gets_the_requested_mode(tmp_path: Path) -> None:
    if not hasattr(__import__("os"), "fchmod"):
        pytest.skip("POSIX file modes")
    path = tmp_path / "mcp.json"

    write_json_config(path, {}, default_trailing_newline=True, new_file_mode=0o600)

    assert path.stat().st_mode & 0o777 == 0o600
