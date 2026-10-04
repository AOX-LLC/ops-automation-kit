import re
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from opskit.config import Settings

API_DIR = Path(__file__).resolve().parents[2] / "src/opskit/api"
CSS = (API_DIR / "static/approver.css").read_text()
TOKEN_NAME = re.compile(r"(--pui-[a-z-]+)\s*:")


def _block_after(opening: str) -> str:
    start = CSS.index(opening)
    body_start = CSS.index("{", start) + 1
    depth, end = 1, body_start
    while depth:
        depth += {"{": 1, "}": -1}.get(CSS[end], 0)
        end += 1
    return CSS[body_start : end - 1]


def _color_tokens(block: str) -> set[str]:
    return {name for name in TOKEN_NAME.findall(block) if name.startswith("--pui-")}


LIGHT = _color_tokens(_block_after(":root {"))
ROLE_TOKENS = {
    name for name in LIGHT if not re.search(r"font|radius|space|control-lg|topbar", name)
}


def test_both_dark_blocks_define_every_role_token_the_light_block_does() -> None:
    by_preference = _color_tokens(_block_after("@media (prefers-color-scheme: dark)"))
    by_attribute = _color_tokens(_block_after(':root[data-theme="dark"]'))
    assert ROLE_TOKENS
    assert by_preference >= ROLE_TOKENS
    assert by_preference == by_attribute


def test_the_dark_blocks_declare_a_dark_color_scheme() -> None:
    assert "color-scheme: dark" in _block_after("@media (prefers-color-scheme: dark)")
    assert "color-scheme: dark" in _block_after(':root[data-theme="dark"]')


def test_the_preference_block_yields_to_an_explicit_light_choice() -> None:
    assert ':root:not([data-theme="light"])' in CSS


def test_no_template_hard_codes_a_theme() -> None:
    for template in (API_DIR / "templates").glob("*.html"):
        assert "data-theme" not in template.read_text(), template.name


def test_the_page_carries_both_logos_and_shows_one_per_theme() -> None:
    env = Environment(
        loader=FileSystemLoader(API_DIR / "templates"), autoescape=select_autoescape()
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=Settings())))
    html = env.get_template("base.html").render(request=request, logged_in=False)
    assert "aox-logo-black.png" in html and "aox-logo-white.png" in html
    assert "pui-logo--light" in html and "pui-logo--dark" in html
    assert (API_DIR / "static/brand/aox-logo-white.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_custom_logo_serves_both_themes_unless_given_a_dark_twin() -> None:
    assert Settings(brand_logo_url="/static/brand/mine.png").brand_logo_dark_url_resolved is None
    mine = Settings(
        brand_logo_url="/static/brand/mine.png", brand_logo_dark_url="/static/brand/mine-dark.png"
    )
    assert mine.brand_logo_dark_url_resolved == "/static/brand/mine-dark.png"
    assert Settings().brand_logo_dark_url_resolved == "/static/brand/aox-logo-white.png"
