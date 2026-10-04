# Terminal clips

Terminal recordings for the portfolio, made with [VHS](https://github.com/charmbracelet/vhs) (tools from `demo/setup-tools.sh` into `demo/.bin`). Each clip renders in a dark and a light theme using the AOX Portfolio UI colours.

## Run

```sh
demo/terminal/render.sh dark evals        # one clip, one theme
demo/terminal/render.sh light evals
```

Output goes to `demo/terminal/out/<theme>/terminal/<clip>.mp4` and `.gif` (set `OUT=` to put `<theme>/terminal/` elsewhere). The MP4 is H.264, crf 27, 30 fps. The GIF is 900 px wide, palette method, 15 fps (10 fps if the first pass is over 3 MB). The raw webm lives in a temp dir and is deleted. `KEEP_WORK=1` keeps the generated tape for debugging.

## How it works

- `clips/<clip>.tape.body`: the clip's script. No settings; only the Hide block, typing and waits.
- `themes.sh`: the two theme JSON blocks.
- `render.sh`: writes `Output`, font, size (1200x640, FontSize 18, Padding 24, Margin 0, no window bar) and the theme in front of the body, runs `vhs`, then encodes with ffmpeg.
- `captions.json`: per-clip caption text and an optional `speed`. Any clip that is sped up says so in its caption, and `render.sh` applies the same factor.
- Font: IBM Plex Mono is not installed on this box, so the clips use DejaVu Sans Mono. Set `FONT="IBM Plex Mono"` once it is installed.

## The no-prompt rule

No real prompt, hostname or path may show on screen. Every body starts with a hidden block:

```
Hide
Type "bash --noprofile --norc" Enter     # a shell with no user rc, no fancy prompt
Type "export PS1='$ ' && cd @REPO@" Enter # prompt is "$ "; @REPO@ is the checkout, hidden
...
Type "clear" Enter
Show
```

`@REPO@` is filled in by `render.sh`, so the absolute path exists only inside the hidden block, and `clear` wipes it from the screen. Commands whose output could print a path or host are filtered (the evals clip prints only the eval's JSON lines, reformatted). After rendering, check it: extract frames and OCR them.

```sh
ffmpeg -i out/dark/terminal/evals.mp4 -vf fps=2 /tmp/fr/f%03d.png
for f in /tmp/fr/*.png; do tesseract "$f" -; done | grep -i -E 'aiden|@|/home|node-01|portfolio|worktree'   # must print nothing
```

## Add a clip

1. Write `clips/<name>.tape.body` starting with the Hide block above.
2. Wait on real output (`Wait+Screen@60s /text/`), never on a fixed sleep for slow commands.
3. Add an entry to `captions.json` (add `speed` if you speed it up).
4. Render both themes, OCR the frames, and read a frame by eye.

## Clips

- `evals`: `make evals` in replay mode (recorded responses, no key, no spend, no stack). It rewrites timestamps in the tracked `evals/scorecards/*.scorecard.json`; `render.sh` runs `git checkout -- evals` afterwards if `git status --porcelain evals` changed, so the tree stays clean. About 12 s, not sped up.

## Not built

A quickstart clip (`docker compose up -d --wait` on a clean boot) was tried and dropped: with the output piped through `grep` and `cut`, VHS saw no output for six minutes and timed out. A wrapper that holds the docker lock, boots clean and records would need compose's own progress output on a TTY (for example `--progress=plain` without the pipe), and would run under `flock ~/portfolio-projects/.locks/docker`.
