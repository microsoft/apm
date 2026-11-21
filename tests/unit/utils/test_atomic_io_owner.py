"""deterministic-atomic-text-writes owner: temp naming, text mode, descriptor ownership (#4121)."""

from __future__ import annotations

import codecs
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.utils.atomic_io import atomic_write_text, write_text_lf

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-deterministic-atomic-text-writes")]


@pytest.mark.parametrize("parameter", ["temp_prefix", "temp_suffix"])
def test_an_unportable_temp_fragment_names_its_parameter(tmp_path: Path, parameter: str) -> None:
    with patch("tempfile.mkstemp") as mkstemp, pytest.raises(ValueError) as caught:
        atomic_write_text(tmp_path / "out.txt", "data", **{parameter: "../x"})
    assert str(caught.value) == f"{parameter} must be a portable filename fragment"
    mkstemp.assert_not_called()


class TestAtomicWriteOwnership:
    """Temp naming, text-mode contract and descriptor ownership on failure."""

    def _capture_temp_name(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        names: list[str] = []
        real = os.replace

        def _record(source: str, destination: Path) -> None:
            names.append(Path(source).name)
            real(source, destination)

        monkeypatch.setattr(os, "replace", _record)
        return names

    def test_default_temp_name_has_prefix_and_no_suffix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        names = self._capture_temp_name(monkeypatch)

        atomic_write_text(tmp_path / "out.txt", "x")

        assert len(names) == 1
        assert re.fullmatch(r"apm-atomic-[a-z0-9_]+", names[0])

    def test_custom_temp_suffix_is_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        names = self._capture_temp_name(monkeypatch)

        atomic_write_text(tmp_path / "out.txt", "x", temp_prefix="p-", temp_suffix=".part")

        assert names[0].startswith("p-")
        assert names[0].endswith(".part")

    def test_text_mode_is_utf8_without_newline_translation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        seen: list[dict] = []
        real_fdopen = os.fdopen

        def _fdopen(fd, *args, **kwargs):
            seen.append(kwargs)
            return real_fdopen(fd, *args, **kwargs)

        monkeypatch.setattr(os, "fdopen", _fdopen)

        atomic_write_text(tmp_path / "out.txt", "a\r\nb")

        assert codecs.lookup(seen[0]["encoding"]).name == "utf-8"
        assert seen[0]["newline"] == ""

    def test_fchmod_failure_is_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        def _fail(fd: int, mode: int) -> None:
            raise OSError("no chmod")

        monkeypatch.setattr(os, "fchmod", _fail, raising=False)
        target = tmp_path / "out.txt"

        atomic_write_text(target, "x", new_file_mode=0o600)

        assert target.read_text(encoding="utf-8") == "x"

    def test_unwrapped_descriptor_is_closed_once_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        closed: list[int] = []
        real_close = os.close

        def _close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)
            raise OSError("close reported an error")

        def _fdopen(*args, **kwargs):
            raise RuntimeError("fdopen failed")

        monkeypatch.setattr(os, "close", _close)
        monkeypatch.setattr(os, "fdopen", _fdopen)

        with pytest.raises(RuntimeError, match="fdopen failed"):
            atomic_write_text(tmp_path / "out.txt", "x")

        assert len(closed) == 1
        assert list(tmp_path.iterdir()) == []

    def test_wrapped_descriptor_is_not_closed_twice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        closed: list[int] = []
        monkeypatch.setattr(os, "close", closed.append)

        with pytest.raises(UnicodeEncodeError):
            atomic_write_text(tmp_path / "out.txt", "\ud800")

        assert closed == []
        assert list(tmp_path.iterdir()) == []


class TestWriteTextLfContract:
    def test_text_mode_is_utf8_without_newline_translation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict] = []
        real = Path.write_text

        def _write_text(self: Path, data: str, **kwargs) -> int:
            seen.append(kwargs)
            return real(self, data, **kwargs)

        monkeypatch.setattr(Path, "write_text", _write_text)

        write_text_lf(tmp_path / "out.txt", "a\r\nb")

        assert codecs.lookup(seen[0]["encoding"]).name == "utf-8"
        assert seen[0]["newline"] == ""
