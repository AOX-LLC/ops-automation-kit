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
strip = _load("strip_media_metadata")


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


def test_a_png_with_an_unlisted_chunk_or_trailing_bytes_fails(tmp_path: Path) -> None:
    profile = tmp_path / "profile.png"
    profile.write_bytes(_png((b"iCCP", b"name\x00\x00data")))
    assert media.check(profile, []) == ["PNG iCCP chunk"]
    trailing = tmp_path / "trailing.png"
    trailing.write_bytes(_png() + b"secret after the end")
    assert "bytes after the PNG IEND chunk" in media.check(trailing, [])


def test_a_text_file_named_png_or_gif_fails(tmp_path: Path) -> None:
    for name in ("note.png", "note.gif", "note.mp4", "note.webm"):
        fake = tmp_path / name
        fake.write_text("just some text, long enough to read")
        assert media.check(fake, []), name


def test_jpeg_and_webp_are_refused_and_the_suffix_check_ignores_case(tmp_path: Path) -> None:
    for name in ("photo.jpg", "photo.JPEG", "photo.webp"):
        assert "not supported" in media.check(tmp_path / name, [])[0]
    upper = tmp_path / "SHOT.PNG"
    upper.write_bytes(_png((b"tEXt", b"k\x00v")))
    assert media.check(upper, []) == ["PNG tEXt chunk"]


def test_a_truncated_gif_is_reported_not_a_traceback(tmp_path: Path) -> None:
    broken = tmp_path / "broken.gif"
    broken.write_bytes(b"GIF89a\x01")
    assert media.check(broken, []) == ["unreadable or malformed: it could not be checked"]


def test_the_gif_plain_text_extension_and_trailing_bytes_fail(tmp_path: Path) -> None:
    screen = b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0, 0, 0)
    plain = tmp_path / "plain.gif"
    plain.write_bytes(screen + b"\x21\x01" + bytes([4]) + b"abcd" + b"\x00" + b"\x3b")
    assert media.check(plain, []) == ["GIF plain-text extension"]
    trailing = tmp_path / "trailing.gif"
    trailing.write_bytes(screen + b"\x3b" + b"hidden")
    assert media.check(trailing, []) == ["bytes after the GIF trailer"]


def test_stripping_a_png_and_a_gif_removes_what_the_check_flags(tmp_path: Path) -> None:
    picture = tmp_path / "tagged.png"
    picture.write_bytes(_png((b"tEXt", b"k\x00v"), (b"iCCP", b"n\x00\x00d")) + b"tail")
    strip.main([str(picture)])
    assert media.check(picture, []) == []
    screen = b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0, 0, 0)
    animation = tmp_path / "commented.gif"
    animation.write_bytes(screen + b"\x21\xfe" + bytes([2]) + b"hi" + b"\x00" + b"\x3b")
    strip.main([str(animation)])
    assert media.check(animation, []) == []
