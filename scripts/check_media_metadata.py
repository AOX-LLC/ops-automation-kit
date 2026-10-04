"""Check the metadata of media files (png, gif, mp4, webm) for anything that could carry a name.

The public-safety hook reads text and skips binary media, because a short term matches the
compressed bytes of a picture or a video by chance. The places a real name could hide in a media
file are its metadata (PNG text chunks, GIF comments, container tags), so this checks those:

1. Structure, always (CI runs it too): a file may carry no free-text metadata. PNG: no tEXt, iTXt,
   zTXt, eXIf or tIME chunk. GIF: no comment or application extension beyond the loop count.
   mp4 and webm: only the technical tags listed in ALLOWED_TAGS.
2. The denylist, when `.denylist.local` exists (it never enters the repository): no term in the
   text of any metadata tag. Without the file, step 1 still holds.

Needs ffprobe for mp4 and webm. Strip a file with scripts/strip_media_metadata.sh.
Prints the file and the rule, never the denylisted term.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

DENYLIST = Path(__file__).resolve().parents[1] / ".denylist.local"
ALLOWED_TAGS = {
    "language",
    "handler_name",
    "vendor_id",
    "major_brand",
    "minor_version",
    "compatible_brands",
    "encoder",
    "duration",
}
FORBIDDEN_PNG_CHUNKS = {b"tEXt", b"iTXt", b"zTXt", b"eXIf", b"tIME"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
VIDEO_SUFFIXES = {".mp4", ".webm"}
SUPPORTED = {".png", ".gif"} | VIDEO_SUFFIXES


def load_terms() -> list[str]:
    if not DENYLIST.is_file():
        return []
    lines = DENYLIST.read_text(encoding="utf-8").splitlines()
    return [line.strip().lower() for line in lines if line.strip() and not line.startswith("#")]


def png_findings(data: bytes) -> tuple[list[str], str]:
    findings, text, position = [], "", len(PNG_SIGNATURE)
    while position + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[position : position + 8])
        body = data[position + 8 : position + 8 + length]
        if kind in FORBIDDEN_PNG_CHUNKS:
            findings.append(f"PNG {kind.decode()} chunk")
            text += body.decode("latin-1", errors="ignore") + "\n"
        position += 12 + length
    return findings, text


def _skip_sub_blocks(data: bytes, position: int) -> tuple[int, bytes]:
    collected = b""
    while position < len(data) and data[position] != 0:
        size = data[position]
        collected += data[position + 1 : position + 1 + size]
        position += 1 + size
    return position + 1, collected


def gif_findings(data: bytes) -> tuple[list[str], str]:
    """Walk the GIF blocks: a comment extension, or an application extension other than the loop
    count (NETSCAPE2.0), can hold text."""
    findings, text = [], ""
    flags = data[10]
    position = 13 + (3 * (2 ** ((flags & 7) + 1)) if flags & 0x80 else 0)
    while position < len(data):
        marker = data[position]
        if marker == 0x3B:  # trailer
            break
        if marker == 0x2C:  # image descriptor
            flags = data[position + 9]
            position += 10 + (3 * (2 ** ((flags & 7) + 1)) if flags & 0x80 else 0) + 1
            position, _ = _skip_sub_blocks(data, position)
        elif marker == 0x21:  # extension
            label = data[position + 1]
            position, body = _skip_sub_blocks(data, position + 2)
            if label == 0xFE:
                findings.append("GIF comment extension")
                text += body.decode("latin-1", errors="ignore") + "\n"
            elif label == 0xFF and not body.startswith((b"NETSCAPE2.0", b"ANIMEXTS1.0")):
                findings.append("GIF application extension")
                text += body.decode("latin-1", errors="ignore") + "\n"
        else:
            break
    return findings, text


def video_findings(path: Path) -> tuple[list[str], str]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise SystemExit("ffprobe is required to check mp4 and webm metadata")
    probe = subprocess.run(  # noqa: S603 - a fixed program and a path from the commit
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format_tags:stream_tags",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    parsed = json.loads(probe.stdout or "{}")
    tag_sets = [parsed.get("format", {}).get("tags", {})]
    tag_sets += [stream.get("tags", {}) for stream in parsed.get("streams", [])]
    findings, text = [], ""
    for tags in tag_sets:
        for key, value in tags.items():
            text += f"{key}={value}\n"
            if key.lower() not in ALLOWED_TAGS:
                findings.append(f"metadata tag {key!r}")
    return findings, text


def check(path: Path, terms: list[str]) -> list[str]:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        return []
    if suffix == ".png":
        findings, text = png_findings(path.read_bytes())
    elif suffix == ".gif":
        findings, text = gif_findings(path.read_bytes())
    else:
        findings, text = video_findings(path)
    lowered = text.lower()
    if any(term in lowered for term in terms):
        findings.append("a denylisted term in the metadata")
    return findings


def main(paths: list[str]) -> int:
    terms, failed = load_terms(), False
    for name in paths:
        for finding in check(Path(name), terms):
            failed = True
            print(f"{name}: {finding}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
