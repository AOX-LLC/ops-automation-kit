"""The public-safety hook skips binary media; the metadata check covers what can carry a name."""

from __future__ import annotations

import importlib.util
import struct
import zlib
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


safety = _load("check_public_safety")
media = _load("check_media_metadata")


def _png(*extra_chunks: tuple[bytes, bytes]) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body)
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    chunks = [chunk(b"IHDR", header)]
    chunks += [chunk(kind, body) for kind, body in extra_chunks]
    chunks += [chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")), chunk(b"IEND", b"")]
    return media.PNG_SIGNATURE + b"".join(chunks)


def test_the_text_hook_skips_binary_media_even_when_the_bytes_contain_a_term(
    tmp_path: Path,
) -> None:
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00abc\x00")
    assert safety.find_hits(picture, ["abc"]) == []


def test_the_text_hook_still_reads_text_files(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text("an abc inside")
    assert safety.find_hits(note, ["abc"]) == ["abc"]


def test_a_clean_png_passes(tmp_path: Path) -> None:
    picture = tmp_path / "clean.png"
    picture.write_bytes(_png())
    assert media.check(picture, []) == []


def test_a_png_text_chunk_is_a_finding_and_its_text_is_searched(tmp_path: Path) -> None:
    picture = tmp_path / "tagged.png"
    picture.write_bytes(_png((b"tEXt", b"Comment\x00made by someone-secret")))
    findings = media.check(picture, ["someone-secret"])
    assert "PNG tEXt chunk" in findings
    assert "a denylisted term in the metadata" in findings


def test_a_gif_comment_is_a_finding(tmp_path: Path) -> None:
    logical_screen = b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0, 0, 0)
    comment = b"\x21\xfe" + bytes([5]) + b"hello" + b"\x00"
    picture = tmp_path / "commented.gif"
    picture.write_bytes(logical_screen + comment + b"\x3b")
    assert media.check(picture, []) == ["GIF comment extension"]
