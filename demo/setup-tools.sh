#!/usr/bin/env bash
# Fetch the pinned terminal-recording tools into demo/.bin (gitignored): VHS and ttyd.
# Linux x86_64 only. Playwright and its browser come from `npm ci` and `npx playwright install`;
# ffmpeg and ffprobe must already be on PATH. Every download is checked against the publisher's
# checksum file before it is used.
set -euo pipefail

VHS_VERSION=0.12.1
TTYD_VERSION=1.7.7
BIN="$(cd "$(dirname "$0")" && pwd)/.bin"
mkdir -p "$BIN"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

fetch() { curl -fsSL --retry 3 -o "$2" "$1"; }

if [ ! -x "$BIN/vhs" ]; then
    base="https://github.com/charmbracelet/vhs/releases/download/v${VHS_VERSION}"
    archive="vhs_${VHS_VERSION}_Linux_x86_64.tar.gz"
    fetch "$base/$archive" "$WORK/$archive"
    fetch "$base/checksums.txt" "$WORK/vhs-sums"
    (cd "$WORK" && grep " $archive\$" vhs-sums | sha256sum -c -)
    tar -xzf "$WORK/$archive" -C "$WORK"
    install -m 0755 "$(find "$WORK" -name vhs -type f | head -1)" "$BIN/vhs"
fi

if [ ! -x "$BIN/ttyd" ]; then
    base="https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}"
    fetch "$base/ttyd.x86_64" "$WORK/ttyd.x86_64"
    fetch "$base/SHA256SUMS" "$WORK/ttyd-sums"
    (cd "$WORK" && grep " ttyd.x86_64\$" ttyd-sums | sha256sum -c -)
    install -m 0755 "$WORK/ttyd.x86_64" "$BIN/ttyd"
fi

for tool in ffmpeg ffprobe; do
    command -v "$tool" >/dev/null || { echo "missing $tool: install it with your package manager" >&2; exit 1; }
done
echo "tools ready: $("$BIN/vhs" --version | head -1), ttyd $("$BIN/ttyd" --version | head -1)"
