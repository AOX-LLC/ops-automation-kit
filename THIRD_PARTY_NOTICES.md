# Third-party notices

This repository's own code is MIT licensed (see [LICENSE](LICENSE)). The items below are not covered by that licence.

## n8n (pulled at runtime, not redistributed)

- What it is: the workflow engine. `compose.yaml` pulls the official `n8nio/n8n` container image (digest-pinned).
- Licence: n8n Sustainable Use License. Terms: <https://docs.n8n.io/sustainable-use-license/>
- Where it lives: nowhere in this repository. No n8n code or image is bundled. The workflow JSON in `n8n/workflows/` is this project's own export format.
- What this means if you copy this repo: you pull n8n directly from its publisher under its own licence, so n8n's terms apply to you, not the MIT licence. The Sustainable Use License limits how n8n may be used and redistributed (broadly: internal business use is allowed; selling n8n or offering it as a hosted service is not). Read the terms before using this kit commercially.

## Receipt fonts (redistributed in the repo)

Used only by the sample generator (`tools/samplegen/receipts.py`) to draw the fictional receipt images in `samples/`. Licence: SIL Open Font License 1.1. The licence text is in [tools/samplegen/fonts/OFL.txt](tools/samplegen/fonts/OFL.txt).

| Font | File | Copyright (from the font's own name table) |
| --- | --- | --- |
| Courier Prime | `tools/samplegen/fonts/CourierPrime-Regular.ttf` | Copyright 2015 The Courier Prime Project Authors (https://github.com/quoteunquoteapps/CourierPrime). |
| Cutive Mono | `tools/samplegen/fonts/CutiveMono-Regular.ttf` | Copyright 2012 The Cutive Project Authors (https://github.com/googlefonts/cutivemono) |

`OFL.txt` carries the Courier Prime copyright line. The Cutive Mono copyright line above is embedded in its font file and states the same licence (SIL OFL 1.1).

## Approver page fonts

The approver page does not self-host any fonts. `src/opskit/api/static/approver.css` names IBM Plex Sans, IBM Plex Mono and Space Grotesk in its font stacks, with system fallbacks, but the repository ships no font files for them and loads none from a remote host. If a viewer has those families installed locally they are used; otherwise the system fallback is used. Nothing to redistribute.

## Container images pulled by `compose.yaml` (not redistributed)

| Image | What it is | Licence |
| --- | --- | --- |
| `pgvector/pgvector` (`0.8.7-pg17-trixie`) | PostgreSQL 17 with the pgvector extension | PostgreSQL (database) and PostgreSQL License (pgvector); the image also contains other open-source packages under their own licences |
| `axllent/mailpit` (`v1.31.3`) | local mail catcher for the approval emails | MIT |

## AOX logo (trademark, not MIT)

`src/opskit/api/static/brand/aox-logo-black.png` is a trademark of AOX LLC and is not licensed under this repository's MIT licence. Do not use it to suggest your deployment comes from or is endorsed by AOX. See [src/opskit/api/static/brand/README.md](src/opskit/api/static/brand/README.md) for how to replace or hide it.
