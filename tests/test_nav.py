"""The nav lives in two places by necessity (Python for the older server-rendered
pages, JavaScript for the pages that build their own header); they must agree."""

import re
from pathlib import Path

import nav
import server

CHROME_JS = Path(__file__).parent.parent / "app" / "static" / "js" / "chrome.js"


def test_chrome_js_lists_the_same_links_in_the_same_order():
    js = CHROME_JS.read_text(encoding="utf-8")
    block = js[js.index("const NAV = ["):js.index("export const BOARD_THEMES")]
    assert re.findall(r"href: '([^']+)'", block) == nav.all_hrefs()
    assert re.findall(r"label: '([^']+)'", block) == [
        label for e in nav.NAV for label in ([e["label"]] + [i["label"] for i in e.get("items", [])])]


def test_render_marks_the_active_link_and_its_group():
    html = nav.render_nav("/clock")
    assert '<a href="/clock" class="active">Clock</a>' in html
    assert html.count("active-section") == 1 and html.index("active-section") < html.index("/clock")
    assert '<a href="/" class="active">' in nav.render_nav("/")
    assert "active" not in nav.render_nav(None).replace("aria-", "")


def test_apply_nav_replaces_only_the_nav_block():
    page = '<header><nav class="top-nav">\n <a href="/old">Old</a>\n</nav><b>keep</b></header>'
    out = nav.apply_nav(page, "/play")
    assert "/old" not in out and "<b>keep</b>" in out and '<a href="/play" class="active">Play</a>' in out
    assert nav.apply_nav("<header id='site-header'></header>", "/") == "<header id='site-header'></header>"


def test_every_nav_target_is_a_registered_page():
    pages = {path for path, _, _ in server.PAGES}
    assert set(nav.all_hrefs()) <= pages
