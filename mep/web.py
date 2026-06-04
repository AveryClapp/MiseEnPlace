"""Fetch a recipe from a web page.

Most recipe sites embed their recipe as schema.org `Recipe` JSON-LD (it is how
Google builds those recipe cards), so we parse that directly: accurate, and no
LLM call needed. When a page has no usable JSON-LD, `parse_page` still returns a
length-capped readable-text blob so the caller can fall back to LLM extraction.

stdlib only (urllib + html.parser + json) to keep the install light.
"""

import json
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .errors import MepError

_UA = "Mozilla/5.0 (compatible; mep/1.0; +https://github.com/AveryClapp/MiseEnPlace)"
_TRACKING = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid",
}
_TEXT_CAP = 20000  # cap fallback page text so a huge page can't blow up token cost


def fetch_recipe(url: str) -> dict:
    """Fetch `url` and return parse_page()'s result. Raises MepError on a fetch
    failure."""
    return parse_page(_get(url), url)


def parse_page(html: str, url: str) -> dict:
    """Pure: turn page HTML into {title, site, recipes, text}. `recipes` is a
    list of extracted-recipe dicts from JSON-LD (possibly empty); `text` is a
    readable fallback for LLM extraction."""
    p = _PageParser()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML shouldn't crash the parse
        pass
    title = " ".join(p.title_parts).strip() or None
    recipes = _recipes_from_ldjson(p.scripts)
    text = " ".join(p.text_parts)[:_TEXT_CAP]
    return {"title": title, "site": _site_name(url), "recipes": recipes, "text": text}


def canonical_url(url: str) -> str:
    """Normalize a web URL for idempotency: lowercase host, drop the fragment,
    strip tracking query params, and remove a trailing slash."""
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https"):
        raise MepError(f"Not a web URL: {url}")
    query = urlencode(
        [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _TRACKING]
    )
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", query, ""))


# --- HTML parsing -------------------------------------------------------------


class _PageParser(HTMLParser):
    """Collect ld+json script bodies, the <title>, and visible text."""

    def __init__(self):
        super().__init__()
        self.scripts: list[str] = []
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._in_ldjson = False
        self._in_title = False
        self._skip_depth = 0  # inside <script>/<style>/<noscript> we ignore text
        self._cur: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self._in_ldjson = True
            self._cur = []
        elif tag in ("script", "style", "noscript"):
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ldjson:
            self._in_ldjson = False
            self.scripts.append("".join(self._cur))
        elif tag in ("script", "style", "noscript") and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_ldjson:
            self._cur.append(data)
        elif self._in_title:
            self.title_parts.append(data)
        elif self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self.text_parts.append(stripped)


def _site_name(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


# --- JSON-LD -> recipe --------------------------------------------------------


def _recipes_from_ldjson(scripts: list[str]) -> list[dict]:
    out = []
    for raw in scripts:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _iter_nodes(data):
            if _is_recipe(node):
                mapped = _map_recipe(node)
                if mapped:
                    out.append(mapped)
    return out


def _iter_nodes(data):
    if isinstance(data, list):
        for item in data:
            yield from _iter_nodes(item)
    elif isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _iter_nodes(item)
        yield data


def _is_recipe(node: dict) -> bool:
    t = node.get("@type")
    if isinstance(t, list):
        return any(str(x).lower() == "recipe" for x in t)
    return str(t).lower() == "recipe"


def _map_recipe(node: dict) -> dict | None:
    name = node.get("name")
    name = name.strip() if isinstance(name, str) else None
    ingredients = [
        {"name": s.strip(), "quantity": None, "unit": None, "prep": None}
        for s in _as_list(node.get("recipeIngredient") or node.get("ingredients"))
        if isinstance(s, str) and s.strip()
    ]
    steps = _instructions(node.get("recipeInstructions"))
    if not name or (not ingredients and not steps):
        return None
    return {
        "dish_name": name,
        "cook_time": _duration(node.get("totalTime") or node.get("cookTime")),
        "servings": _yield(node.get("recipeYield")),
        "difficulty": None,
        "ingredients": ingredients,
        "steps": steps,
        "tags": _tags(node.get("recipeCategory"), node.get("recipeCuisine")),
    }


def _instructions(value) -> list[str]:
    """Flatten schema.org recipeInstructions into ordered step strings. Handles a
    plain string (split on newlines), a list of strings, HowToStep objects, and
    HowToSection objects with nested itemListElement."""
    steps: list[str] = []

    def add(v):
        if isinstance(v, str):
            for line in re.split(r"[\r\n]+", v):
                line = line.strip()
                if line:
                    steps.append(line)
        elif isinstance(v, list):
            for item in v:
                add(item)
        elif isinstance(v, dict):
            if v.get("itemListElement"):
                add(v["itemListElement"])
            else:
                text = v.get("text") or v.get("name")
                if isinstance(text, str) and text.strip():
                    steps.append(text.strip())

    add(value)
    return steps


_DURATION = re.compile(
    r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", re.IGNORECASE
)


def _duration(value) -> str | None:
    """ISO-8601 duration (e.g. 'PT1H30M') -> a human string ('1 hr 30 min')."""
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    m = _DURATION.fullmatch(value.strip())
    if not m:
        return None
    days, hours, minutes, _seconds = (int(x) if x else 0 for x in m.groups())
    total = days * 1440 + hours * 60 + minutes
    if total <= 0:
        return None
    hrs, mins = divmod(total, 60)
    parts = []
    if hrs:
        parts.append(f"{hrs} hr")
    if mins:
        parts.append(f"{mins} min")
    return " ".join(parts)


def _yield(value) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _tags(*values) -> list[str]:
    out: list[str] = []
    for value in values:
        for item in _as_list(value):
            if isinstance(item, str) and item.strip():
                tag = item.strip().lower()
                if tag not in out:
                    out.append(tag)
    return out[:6]


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise MepError(f"Couldn't fetch {url}: {exc}")
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")
