#!/usr/bin/env bash
# Render one terminal clip in one theme: demo/terminal/render.sh <dark|light> <clip>
# Writes $OUT/<theme>/terminal/<clip>.mp4 and .gif (OUT defaults to demo/terminal/out). No webm is kept.
set -euo pipefail

theme=${1:?usage: render.sh <dark|light> <clip>}
clip=${2:?usage: render.sh <dark|light> <clip>}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)
bin=$(cd "$here/.." && pwd)/.bin
out=${OUT:-$here/out}/$theme/terminal
body=$here/clips/$clip.tape.body
[ -f "$body" ] || { echo "no clip $clip (looking for $body)" >&2; exit 1; }
export PATH="$bin:$PATH"
# shellcheck source=themes.sh
. "$here/themes.sh"

work=$(mktemp -d)
[ -n "${KEEP_WORK:-}" ] || trap 'rm -rf "$work"' EXIT
[ -z "${KEEP_WORK:-}" ] || echo "keeping $work" >&2
mkdir -p "$out"

# Tape = shared settings + the clip body. The repo path appears only inside the Hide block.
{
  echo 'Output "'"$work/$clip.webm"'"'
  echo 'Set Shell "bash"'
  echo 'Set FontFamily "'"${FONT:-DejaVu Sans Mono}"'"'
  echo 'Set FontSize 18'
  echo 'Set Width 1200'
  echo 'Set Height 640'
  echo 'Set Padding 24'
  echo 'Set Margin 0'
  echo 'Set Framerate 30'
  echo 'Set TypingSpeed 45ms'
  echo "Set Theme '$(theme_json "$theme" | tr -d '\n')'"
  sed "s|@REPO@|$repo|g" "$body"
} > "$work/$clip.tape"

before=$(git -C "$repo" status --porcelain evals)
(cd "$repo" && vhs "$work/$clip.tape")
# Replay evals rewrite tracked scorecards (timestamps only) and may still be writing when the
# recording ends. Let them finish, then put the files back so the tree is as it was.
for _ in $(seq 20); do pgrep -f opskit.evals >/dev/null || break; sleep 1; done
if [ -z "$before" ] && [ -n "$(git -C "$repo" status --porcelain evals)" ]; then
  git -C "$repo" checkout -- evals
  echo "restored evals/ after the run" >&2
fi

speed=$(jq -r --arg c "$clip" '.[$c].speed // 1' "$here/captions.json")
pts="setpts=PTS/$speed"
ffmpeg -v error -y -i "$work/$clip.webm" -vf "$pts,fps=30,format=yuv420p" \
  -c:v libx264 -crf 27 -preset slow -movflags +faststart -an "$out/$clip.mp4"

# GIF: 900px wide, palette method; halve the frame rate once if it lands over 3 MB.
for fps in 15 10; do
  ffmpeg -v error -y -i "$work/$clip.webm" \
    -vf "$pts,fps=$fps,scale=900:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=none" \
    "$out/$clip.gif"
  [ "$(stat -c%s "$out/$clip.gif")" -le 3145728 ] && break
done
ls -l "$out/$clip".{mp4,gif}
