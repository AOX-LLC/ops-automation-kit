#!/usr/bin/env bash
# Strip the metadata from media files in place, without re-encoding (the picture and sound bytes
# are copied as they are): scripts/strip_media_metadata.sh <file>...
# Works on png, gif, mp4 and webm. Check the result with scripts/check_media_metadata.py.
set -euo pipefail
for file in "$@"; do
    tmp="$file.stripping.${file##*.}"
    ffmpeg -v error -y -i "$file" -map 0 -map_metadata -1 -map_chapters -1 -c copy \
        -fflags +bitexact -flags:v +bitexact -flags:a +bitexact "$tmp"
    mv "$tmp" "$file"
done
