#!/usr/bin/env bash
# Copy the finished media from demo/out into docs/media under the names the README uses.
#   demo/publish.sh [--reviewed] [--canvas-only]
# Run after `node edit.ts light`, `node edit.ts dark` and the terminal clips. Raw recordings never
# leave demo/out. The files are staged in a temporary folder first: stripped of metadata, checked
# for metadata (scripts/check_media_metadata.py) and OCR-checked for names, hosts and prompts
# (check-frames.ts). Only if all of that passes are they moved into docs/media. OCR misreads
# happen: read each finding, and rerun with --reviewed to accept the frame check's findings this
# once (the metadata check cannot be waived).
# MEDIA_OUT names a folder outside the repo for the full videos, which go to YouTube, not git;
# they get the same checks.
set -euo pipefail
cd "$(dirname "$0")"
reviewed=0; canvas_only=0
for arg in "$@"; do
    case "$arg" in --reviewed) reviewed=1 ;; --canvas-only) canvas_only=1 ;; *) echo "unknown option: $arg" >&2; exit 2 ;; esac
done
OUT=out
DEST=../docs/media
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/loops" "$STAGE/terminal" "$STAGE/videos"

for theme in light dark; do
    # The canvas stills come from canvas-stills.sh, which needs no video; --canvas-only publishes just those.
    for still in canvas-receipts canvas-inbox canvas-leads; do
        cp "$OUT/$theme/stills-web/$still.png" "$STAGE/$still-$theme.png"
    done
    [ "$canvas_only" = 1 ] && continue
    cp "$OUT/$theme/walkthrough.mp4" "$STAGE/videos/walkthrough-$theme.mp4"
    cp "$OUT/$theme/readme.gif" "$STAGE/walkthrough-$theme.gif"
    for still in approver-detail reconciliation crm-records; do
        cp "$OUT/$theme/stills-web/$still.png" "$STAGE/$still-$theme.png"
    done
    for loop in "$OUT/$theme"/loops/*.mp4 "$OUT/$theme"/loops/*.webm; do
        base=$(basename "$loop"); cp "$loop" "$STAGE/loops/${base%.*}-$theme.${base##*.}"
    done
    for clip in "$OUT/$theme"/terminal/*.mp4 "$OUT/$theme"/terminal/*.gif; do
        [ -e "$clip" ] || continue
        base=$(basename "$clip"); cp "$clip" "$STAGE/terminal/${base%.*}-$theme.${base##*.}"
    done
done
# The captions are the same in both themes.
if [ "$canvas_only" = 0 ]; then
cp "$OUT/light/walkthrough.vtt" "$STAGE/walkthrough.vtt"
cp "$OUT/light/walkthrough.srt" "$STAGE/walkthrough.srt"
cp "$OUT/light/cards/social.png" "$STAGE/social-preview.png"
fi

media=$(find "$STAGE" -type f \( -name '*.mp4' -o -name '*.gif' -o -name '*.png' -o -name '*.webm' \) | sort)
python3 ../scripts/strip_media_metadata.py $media
python3 ../scripts/check_media_metadata.py $media
if ! node check-frames.ts $media; then
    [ "$reviewed" = 1 ] || { echo "frame check found something: read it, then rerun with --reviewed" >&2; exit 1; }
fi

# Everything passed: move the files into place. The videos leave the repo.
mkdir -p "$DEST/loops" "$DEST/terminal"
cp -r "$STAGE/loops/." "$DEST/loops/"; cp -r "$STAGE/terminal/." "$DEST/terminal/"
find "$STAGE" -maxdepth 1 -type f -exec cp {} "$DEST/" \;
if [ -n "${MEDIA_OUT:-}" ] && [ "$canvas_only" = 0 ]; then mkdir -p "$MEDIA_OUT"; cp "$STAGE"/videos/*.mp4 "$MEDIA_OUT/"; cp "$STAGE/walkthrough.vtt" "$STAGE/walkthrough.srt" "$MEDIA_OUT/"; fi
du -sh "$DEST"; find "$DEST" -type f | wc -l
