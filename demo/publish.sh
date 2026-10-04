#!/usr/bin/env bash
# Copy the finished media from demo/out into docs/media under the names the README uses.
# Run after `node edit.ts light`, `node edit.ts dark` and the terminal clips, and after
# `node check-frames.ts --all` came back clean. Raw recordings never leave demo/out.
# It then runs the frame check on what it copied and stops on any finding; OCR misreads happen,
# so read each finding, and set MEDIA_REVIEWED=1 to keep the files once a person has.
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
if ! node check-frames.ts $(find "$DEST" -type f \( -name '*.mp4' -o -name '*.gif' -o -name '*.png' -o -name '*.webm' \) | sort); then
    [ "${MEDIA_REVIEWED:-0}" = "1" ] || { echo "frame check found something: look at it, then rerun with MEDIA_REVIEWED=1" >&2; exit 1; }
fi
du -sh "$DEST"; find "$DEST" -type f | wc -l
