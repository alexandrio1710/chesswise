"""One definition of the site navigation.

The pages built on static/js/chrome.js (Game Review, Insights, Puzzles, Skills,
Play) render their header in the browser from its own NAV list; the older pages
have the nav written into their HTML. Both used to be edited by hand in a dozen
places every time a page was added. This is the source of truth for the older
pages — the server rewrites their `<nav class="top-nav">` block on the way out —
and tests/test_nav.py checks that chrome.js lists exactly the same links.
"""

from __future__ import annotations

import re
from html import escape

# (label, href) leaves; a group has "items" and no href of its own.
NAV: list[dict] = [
    {"label": "Dashboard", "href": "/"},
    {"label": "Puzzles", "href": "/puzzles"},
    {"label": "Play", "href": "/play"},
    {"label": "Skills", "href": "/skills"},
    {"label": "Train", "items": [
        {"label": "Explorer", "href": "/explorer"},
        {"label": "Endgame", "href": "/endgame"},
        {"label": "Analyze", "href": "/analyze"},
    ]},
    {"label": "Insights", "items": [
        {"label": "Insights", "href": "/insights"},
        {"label": "Clock", "href": "/clock"},
        {"label": "Progress", "href": "/progress"},
        {"label": "Coaching Report", "href": "/coaching-report"},
    ]},
    {"label": "Search", "href": "/search"},
]


def all_hrefs() -> list[str]:
    hrefs = []
    for entry in NAV:
        hrefs.extend(i["href"] for i in entry["items"]) if "items" in entry else hrefs.append(entry["href"])
    return hrefs


def render_nav(active: str | None) -> str:
    """The inner HTML of `<nav class="top-nav">`, with the link for `active`
    (a path such as "/insights") highlighted."""
    parts = []
    for entry in NAV:
        if "items" not in entry:
            cls = ' class="active"' if entry["href"] == active else ""
            parts.append(f'<a href="{entry["href"]}"{cls}>{escape(entry["label"])}</a>')
            continue
        on = any(i["href"] == active for i in entry["items"])
        links = "".join(
            f'<a href="{i["href"]}"{" class=" + chr(34) + "active" + chr(34) if i["href"] == active else ""}>{escape(i["label"])}</a>'
            for i in entry["items"])
        parts.append(
            '<div class="nav-dropdown">'
            f'<button type="button" class="nav-dropdown-toggle{" active-section" if on else ""}" aria-haspopup="true" aria-expanded="false">'
            f'{escape(entry["label"])} ▾</button><div class="nav-dropdown-menu">{links}</div></div>')
    return "".join(parts)


_NAV_RE = re.compile(r'<nav class="top-nav">.*?</nav>', re.S)


def apply_nav(html: str, active: str | None) -> str:
    """Swap the hand-written nav in an older page for the shared one. Pages that
    build their header in JS have no such block and come back unchanged."""
    return _NAV_RE.sub(lambda m: f'<nav class="top-nav">{render_nav(active)}</nav>', html, count=1)
