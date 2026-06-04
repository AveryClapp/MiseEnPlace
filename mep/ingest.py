"""Orchestration: pull content from a source, extract recipes, classify, store.

Sources share one tail (`_store_recipes`): extract -> per-recipe classify ->
insert. They differ only in how they get the text and a stable id. `add_source`
dispatches a positional argument (a file, a YouTube URL, or a web URL); `add_text`
and `add_video`/`add_channel` are the typed entry points.
"""

import hashlib
import os
import time

from . import db, web, youtube
from .classify import classify_recipe
from .config import require
from .errors import MepError
from .extract import extract_recipes
from .transcript import extract_video_id, fetch_transcript

THROTTLE_SECONDS = 0.5

# Stored when a video has no transcript or is not a recipe: a valid, empty row.
_EMPTY = {"dish_name": None, "ingredients": [], "steps": [], "tags": []}

# A source may yield more than one independent dish. The first recipe keeps the
# real source id (the idempotency anchor); extras get a '#N' suffix so the UNIQUE
# constraint holds. Checking the base id alone still tells us the whole source
# was already ingested.
Result = tuple[int, str | None, str | None, int | None]
# (recipe_id, dish_name, meal_type, health_score)


def _store_recipes(
    conn, config, *, source_id, source_type, title, channel, url, raw_text, recipes
) -> list[Result]:
    """Insert each extracted recipe (an empty list stores one stub), classifying
    the real ones. Returns the list of per-recipe Results."""
    recipes = recipes or [_EMPTY]
    results: list[Result] = []
    for i, extracted in enumerate(recipes):
        sid = source_id if i == 0 else f"{source_id}#{i + 1}"
        recipe_id = db.insert_recipe(
            conn,
            video_id=sid,
            title=title,
            channel=channel,
            url=url,
            raw_transcript=raw_text if i == 0 else None,
            extracted=extracted,
            source_type=source_type,
        )
        meal_type = health = None
        if extracted.get("dish_name"):
            cls = classify_recipe(db.get_recipe(conn, recipe_id), config=config)
            meal_type, health = cls["meal_type"], cls["health_score"]
            db.save_classification(conn, recipe_id, meal_type, health)
        results.append((recipe_id, extracted.get("dish_name"), meal_type, health))
    return results


def ingest_one(conn, config, video_id, title, channel) -> tuple[str, list[Result]]:
    """Ingest a single YouTube video. Returns (status, results); status is one of
    'added', 'no_transcript'. No-transcript and non-recipe videos are stored as
    empty stubs so channel syncs don't re-fetch them."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    transcript = fetch_transcript(video_id)
    if transcript is None:
        results = _store_recipes(
            conn, config, source_id=video_id, source_type="youtube",
            title=title, channel=channel, url=url, raw_text=None, recipes=[],
        )
        return "no_transcript", results

    recipes = extract_recipes(transcript, title=title, config=config)
    results = _store_recipes(
        conn, config, source_id=video_id, source_type="youtube",
        title=title, channel=channel, url=url, raw_text=transcript, recipes=recipes,
    )
    return "added", results


def add_video(conn, config, url) -> tuple[str, list[Result]]:
    """Ingest one video by URL. Returns (status, results); status may also be
    'skipped' (with an empty list) when the video is already stored."""
    video_id = extract_video_id(url)
    if db.video_exists(conn, video_id):
        return "skipped", []
    meta = youtube.fetch_oembed(video_id)
    return ingest_one(conn, config, video_id, meta["title"], meta["channel"])


def add_url(conn, config, url) -> tuple[str, list[Result]]:
    """Ingest a recipe web page. Prefers embedded schema.org JSON-LD; falls back
    to LLM extraction over the page text. Raises if no recipe is found."""
    source_id = web.canonical_url(url)
    if db.video_exists(conn, source_id):
        return "skipped", []
    page = web.fetch_recipe(url)
    recipes = page["recipes"]
    if not recipes:
        recipes = extract_recipes(page["text"], title=page["title"], config=config)
    if not recipes:
        raise MepError(f"No recipe found at {url}.")
    results = _store_recipes(
        conn, config, source_id=source_id, source_type="web",
        title=page["title"], channel=page["site"], url=url,
        raw_text=page["text"] or None, recipes=recipes,
    )
    return "added", results


def add_text(conn, config, text, title=None) -> tuple[str, list[Result]]:
    """Ingest pasted recipe text. The id is a hash of the text, so re-pasting the
    same recipe is a no-op skip. Raises if no recipe is found."""
    text = (text or "").strip()
    if not text:
        raise MepError("No text provided.")
    source_id = "text:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if db.video_exists(conn, source_id):
        return "skipped", []
    recipes = extract_recipes(text, title=title, config=config)
    if not recipes:
        raise MepError("No recipe found in that text.")
    results = _store_recipes(
        conn, config, source_id=source_id, source_type="text",
        title=title, channel=None, url=None, raw_text=text, recipes=recipes,
    )
    return "added", results


def add_source(conn, config, source) -> tuple[str, list[Result]]:
    """Dispatch a positional add argument: a local file (read as text), a YouTube
    URL, or a web URL."""
    if os.path.isfile(source):
        try:
            text = open(source, encoding="utf-8", errors="replace").read()
        except OSError as exc:
            raise MepError(f"Couldn't read {source}: {exc}")
        return add_text(conn, config, text, title=os.path.basename(source))
    if _is_youtube(source):
        return add_video(conn, config, source)
    if source.startswith(("http://", "https://")):
        return add_url(conn, config, source)
    raise MepError(
        "Not a recognized URL or file. Pass a YouTube or recipe URL, a file "
        "path, or use --text."
    )


def _is_youtube(source) -> bool:
    try:
        extract_video_id(source)
        return True
    except MepError:
        return False


def add_channel(conn, config, handle, limit=None):
    """Generator of (video_id, title, status, results) per video processed, so
    the CLI can report progress live. results is the list of (recipe_id,
    dish_name) for that video ([] when skipped). Idempotent and throttled."""
    api_key = require(config, "YOUTUBE_API_KEY")
    uploads, channel_title = youtube.resolve_channel(api_key, handle)
    first = True
    for video_id, title in youtube.iter_playlist_videos(api_key, uploads, limit):
        if db.video_exists(conn, video_id):
            yield video_id, title, "skipped", []
            continue
        if not first:
            time.sleep(THROTTLE_SECONDS)
        first = False
        status, results = ingest_one(conn, config, video_id, title, channel_title)
        yield video_id, title, status, results
