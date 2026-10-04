# Walkthrough pipeline

One recording of the running stack becomes everything the proof kit needs: a 2 to 3 minute video with captions, a GIF for the README, website loop clips, screenshots and terminal clips. Each is made in a light and a dark theme. This project built it first; the other portfolio projects copy it.

```
walkthrough.ts (Playwright, 1440x900, video on)
      | raw.webm + timeline.json + stills/        -> out/<theme>/
      v
edit.ts (ffmpeg) + cards.ts (title, end, social)  -> walkthrough.mp4/.srt/.vtt, readme.gif, loops/, stills-web/
      |
terminal/ (VHS)                                    -> terminal/*.mp4, *.gif
      v
check-frames.ts (OCR every frame)                  -> fails on hostnames, IPs, prompts, internal names
```

Raw recordings and edited output stay in `demo/out/` (gitignored). Only the compressed results go to `docs/media/`.

## Run it

Needs Docker with Compose, Node 22.18 or newer, ffmpeg, ffprobe and tesseract on PATH.

```sh
cd demo
npm ci
npm run setup            # VHS and ttyd (pinned versions and SHA-256) into .bin/, and Chromium for Playwright
./record.sh              # clean seeded stack per theme, records both, stops the stack
node edit.ts light       # and: node edit.ts dark
./terminal/render.sh dark evals       # a terminal clip (see terminal/README.md)
node check-frames.ts --all
```

`record.sh` takes the shared Docker lock around the whole boot, record and stop sequence, starts every theme from `docker compose down -v` (a workflow processes its pending items once, so a second take needs fresh data), and turns the 15 and 5 minute schedules off with `KIT_SCHEDULED_RUNS=false` so every run happens when the script triggers it. The passwords come from `kit-login --env` into the script's environment and are never printed. Logins happen in a throwaway browser context before recording starts, so nothing secret is typed on camera.

The stack runs in replay mode, so a recording costs nothing and needs no key.

## What `walkthrough.ts` shows

1. **Receipts:** the n8n canvas, a run, the execution with every node green, the reconciled spreadsheet with mismatches first, and the summary email.
2. **Inbox:** the canvas, a run, the three injection emails taking the held branch, the summary email with the evidence, the drafts waiting on the approver page, one approved, and the reply arriving in Mailpit.
3. **Leads:** the canvas, a run, the CRM records with the quoted text and source behind each field, and the summary email.

Two scenes show data a browser cannot open (the `.xlsx` and the CRM tables). `pages.ts` renders them from the real export and the real tables in the portfolio tokens, and each page says where its data came from. Nothing on them is typed in by hand.

## Themes

The approver page follows `prefers-color-scheme`, and the recorder sets the colour scheme of the browser context. n8n and Mailpit keep their theme in the browser's local storage; the recorder sets it before the first paint. The title cards, end cards and the demo pages use the AOX Portfolio UI tokens for both themes.

## Reusing it in another project

Copy `demo/` and change only:

| File | Change |
| --- | --- |
| `walkthrough.ts` | Your app's scenes. Keep the `scene({...}, async () => {...})` shape: it writes `timeline.json`, which is the only contract the edit half reads. |
| `config.json` | Title, subtitle, one-liners, run command, repo URL and the logo paths. |
| `templates/card.html` | Card layout, only if the look must change. |
| `record.sh` | The commands that boot your stack and print its login. |
| `pages.ts` | Delete it, or render your own data pages. |
| `terminal/clips/*.tape.body` | Your terminal clips. |

The contract, every command and every knob are in [EDITING.md](EDITING.md). Terminal clips are in [terminal/README.md](terminal/README.md).

Rules the pipeline enforces, and the project must keep:

- Seeded sample data only, with a "Sample data" label visible. No real people, hosts or addresses.
- No hostnames, IPs, shell prompts, internal tools or other projects' names on screen. `check-frames.ts` checks every frame by OCR; a clean run is evidence, not proof, so look at the contact sheets too.
- Anything that starts containers runs under the shared lock and stops them afterwards.
- No live model calls. Record in replay.

## Third-party tooling

Playwright, VHS, ttyd and the Fontsource packages are used to make the media and are not part of the kit. See [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
