#!/usr/bin/env bash
# Copy the finished media from demo/out into docs/media under the names the README uses.
# Run after `node edit.ts light`, `node edit.ts dark` and the terminal clips, and after
# `node check-frames.ts --all` came back clean. Raw recordings never leave demo/out.
set -euo pipefail
cd "$(dirname "$0")"
OUT=out
DEST=../docs/media
mkdir -p "$DEST/loops" "$DEST/terminal"

for theme in light dark; do
    cp "$OUT/$theme/walkthrough.mp4" "$DEST/walkthrough-$theme.mp4"
    cp "$OUT/$theme/readme.gif" "$DEST/walkthrough-$theme.gif"
    for still in approver-detail reconciliation canvas-receipts crm-records; do
        cp "$OUT/$theme/stills-web/$still.png" "$DEST/$still-$theme.png"
    done
    for loop in "$OUT/$theme"/loops/*.mp4 "$OUT/$theme"/loops/*.webm; do
        base=$(basename "$loop"); cp "$loop" "$DEST/loops/${base%.*}-$theme.${base##*.}"
    done
    for clip in "$OUT/$theme"/terminal/*.mp4 "$OUT/$theme"/terminal/*.gif; do
        [ -e "$clip" ] || continue
        base=$(basename "$clip"); cp "$clip" "$DEST/terminal/${base%.*}-$theme.${base##*.}"
    done
done
# The captions are the same in both themes.
cp "$OUT/light/walkthrough.vtt" "$DEST/walkthrough.vtt"
cp "$OUT/light/walkthrough.srt" "$DEST/walkthrough.srt"
cp "$OUT/light/cards/social.png" "$DEST/social-preview.png"
du -sh "$DEST"; find "$DEST" -type f | wc -l
