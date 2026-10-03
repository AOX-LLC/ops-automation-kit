aox-logo-black.png is the AOX logo for the light theme: top left, 20px tall, then a divider and the page name. Don't recolour it or its lime dots.

**The AOX logo is a trademark of AOX LLC and is not licensed under this repository's MIT licence.** It must be replaced in other deployments: put your own logo in this folder and set `OPSKIT_BRAND_LOGO_URL` to its path, or set `OPSKIT_BRAND_LOGO_URL` to empty to hide the logo. The URL must be same-origin (for example a path under `/static/`): the page's content security policy blocks an external URL.
