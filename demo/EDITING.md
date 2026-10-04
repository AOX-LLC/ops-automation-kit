# Editing the walkthrough

The pipeline has two halves. A recording script (`walkthrough.ts`) drives the app and writes raw material to `out/<theme>/`. The edit half in this document turns that into published media and checks it for text that must not ship. To reuse it in another project, copy `demo/` and rewrite only `walkthrough.ts`, `config.json` and the text in `templates/card.html`.

Needs Node 22.18 or newer (it runs `.ts` files directly), Playwright with Chromium, ffmpeg, ffprobe and tesseract on PATH. Everything is written under `demo/out/`, which is gitignored.

## Input contract

For each theme (`light` or `dark`) the recorder leaves, in `out/<theme>/`:

- `raw.webm`: the Playwright video, 1440x900.
- `stills/<name>.png`: full-page stills, 2880x1800 (device pixel ratio 2).
- `timeline.json`:

```json
{
  "project": "ops-automation-kit",
  "theme": "light",
  "scenes": [
    { "id": "receipts-done", "workflow": "receipts", "startMs": 11200, "endMs": 17400,
      "kind": "show", "speed": 1, "caption": "Mismatches are flagged", "loop": "receipts-reconcile", "gif": true }
  ],
  "stills": [{ "name": "receipts", "file": "stills/receipts.png" }]
}
```

| Field | Meaning |
| --- | --- |
| `startMs`, `endMs` | Offsets into `raw.webm`. `endMs` must be after `startMs`. |
| `kind` | `show` plays at normal speed. `wait` marks dead time (a spinner, a model call). |
| `speed` | Playback factor, 1 or more. Use 1 for `show` and something like 8 for `wait`. |
| `caption` | Subtitle shown while the scene plays. An empty string means no cue. |
| `loop` | Optional name. The scene also becomes a website loop clip `loops/<name>`. Letters, digits, `-` and `_` only. |
| `gif` | Optional. The scene is included in the README GIF, in timeline order. |

## Commands

Run from `demo/`.

| Command | What it does |
| --- | --- |
| `node cards.ts [theme ...]` | Renders the title card, end card and social preview for the given themes (both by default). Needs the logo files named in `config.json`; it stops with a clear error if one is missing. |
| `node edit.ts <theme>` | Renders the cards, then builds everything below. |
| `node edit.ts <theme> --only full\|gif\|loops\|stills` | Builds one part. `gif` and `loops` and `stills` do not need the cards. |
| `node check-frames.ts <paths...>` | OCRs frames of the given mp4, gif, webm and png files. |
| `node check-frames.ts --all` | Checks everything under `out/` and `docs/media/`. |
| `node check-frames.ts --selftest` | Proves the checker flags a fabricated bad image and passes a clean one. |
| `node fixtures/make-fake-recording.ts [theme ...]` | Fabricates `raw.webm`, `timeline.json` and two stills so the whole chain runs without the app. |

## Outputs

All under `out/<theme>/`.

| Path | What it is |
| --- | --- |
| `cards/title.png`, `cards/end.png` | 1440x900 cards. |
| `cards/social.png` | 1280x640 social preview. The light one is the committed default. |
| `walkthrough.mp4` | Title card (3 s), scenes, end card (4 s). 1440x900, 30 fps, H.264 yuv420p, crf 27, preset slow, faststart, no audio. 0.3 s crossfades at the two card boundaries and hard cuts between scenes. |
| `walkthrough.srt`, `walkthrough.vtt` | Captions. Cue times come from the edited timeline, so speed-ups, cards and crossfades are accounted for. The same cues are embedded in the mp4 as a soft `mov_text` track (not burned in). |
| `readme.gif` | Scenes with `gif: true`, 800 px wide, 12 fps, two-pass palette, loops forever. Over 4 MB it steps down fps, width and colours until it fits. |
| `loops/<name>.mp4`, `.webm` | 960x600, silent, 5 to 8 s. Scenes longer than 8 s are sped up to 8 s; shorter than 5 s hold the last frame. mp4 starts at crf 30 and webm (VP9) at crf 38, raising crf until each is under 700 KB. |
| `stills-web/<name>.png` | Stills downscaled to 1440 wide. Quantised to 256 colours only when the plain PNG is over 250 KB and SSIM stays at 0.995 or better. The 2x originals in `stills/` are never overwritten. |
| `contact/<name>.png` | Contact sheets (6x4, 320 px thumbnails) from `check-frames.ts`, for a human to look at. |

## Knobs

- Pace: change `speed` on a scene. Dead time at 8 or more keeps the cut short without hiding it.
- Captions: edit `caption`. Leave it empty to show none.
- Website loops: add `loop` to a scene. Remove it to stop producing the clip.
- README GIF: set `gif: true` on the scenes it should contain. Keep it to roughly 10 to 15 seconds in total.
- Card length and card text: `titleCardSeconds`, `endCardSeconds` and the text fields in `config.json`.
- Card look: `templates/card.html` holds the theme tokens at the top of its stylesheet.
- Size limits and quality steps: constants at the top of `edit.ts` (`GIF_LIMIT_BYTES`, `LOOP_LIMIT_BYTES`, `GIF_STEPS` and the crf lists in `makeLoop`).

## The hygiene check

`check-frames.ts` takes one frame per second from each video or gif, plus the first and last, and the whole image for a png (upscaled 2x when narrower than 2000 px). It runs tesseract in `--psm 11` and `--psm 6` and keeps the union of lines. It flags:

- IPv4 addresses other than `127.0.0.1`.
- Hostnames and URLs other than `localhost`, `127.0.0.1`, `*.example`, the repo URL and `api` (the Compose service name, public in `compose.yaml`, shown in n8n's node subtitles).
- Shell prompts: a line starting with `$ `, or containing `user@host`, `~/`, `/home/` or `/Users/`. A line starting with `# ` or `%` is not flagged: it is mostly UI text and icons that OCR misreads, and a root or zsh prompt carries `user@host` or a path anyway. Prompts that start with an entry of `check.allowedPromptPrefixes` in `config.json` are allowed, for terminal clips that show the documented commands on purpose.
- Internal names: every non-blank, non-comment line of `../.denylist.local` (gitignored, the same list the public-safety pre-commit hook uses) as a case-insensitive substring, plus any comma-separated terms in `CHECK_EXTRA_TERMS`. Hits print only the term number, never the line. The list is not in the repo on purpose: a public file naming internal tools would leak them. Without that file this rule checks nothing, so keep it in each working copy.

It prints a table of findings and exits 1 if there are any. OCR can misread, so a clean run is evidence, not proof: look at the contact sheets too. Fast-forwarded `wait` scenes are sampled once per second of the edited video, so check `raw.webm` as well when the app shows anything in a wait.
