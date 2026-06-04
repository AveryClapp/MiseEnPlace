"""Orchestration: pull transcript, extract with Claude, store.

ingest_one is the single unit of work. add_video wraps it with oEmbed metadata
for a one-off URL; add_channel loops it over a channel's uploads, skipping
videos already stored and throttling transcript requests.
"""

import time

from . import db, youtube
from .config import require
from .extract import extract_recipes
from .transcript import extract_video_id, fetch_transcript

THROTTLE_SECONDS = 0.5

# Stored when a video has no transcript or is not a recipe: a valid, empty row.
_EMPTY = {"dish_name": None, "ingredients": [], "steps": [], "tags": []}

# A video may teach more than one independent dish. The first recipe keeps the
# real video_id (the idempotency anchor); extras get a '#N' suffix so the UNIQUE
# constraint holds. Checking the base video_id alone still tells us the whole
# video was already ingested.
Result = tuple[int, str | None]  # (recipe_id, dish_name)


def ingest_one(conn, config, video_id, title, channel) -> tuple[str, list[Result]]:
    """Ingest a single video. Returns (status, results) where results is a list
    of (recipe_id, dish_name). status is one of: 'added', 'no_transcript'."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    transcript = fetch_transcript(video_id)

    if transcript is None:
        recipe_id = db.insert_recipe(
            conn,
            video_id=video_id,
            title=title,
            channel=channel,
            url=url,
            raw_transcript=None,
            extracted=_EMPTY,
        )
        return "no_transcript", [(recipe_id, None)]

    recipes = extract_recipes(transcript, title=title, config=config) or [_EMPTY]
    results: list[Result] = []
    for i, extracted in enumerate(recipes):
        vid = video_id if i == 0 else f"{video_id}#{i + 1}"
        recipe_id = db.insert_recipe(
            conn,
            video_id=vid,
            title=title,
            channel=channel,
            url=url,
            raw_transcript=transcript if i == 0 else None,
            extracted=extracted,
        )
        results.append((recipe_id, extracted.get("dish_name")))
    return "added", results


def add_video(conn, config, url) -> tuple[str, list[Result]]:
    """Ingest one video by URL. Returns (status, results); status may also be
    'skipped' (with an empty list) when the video is already stored."""
    video_id = extract_video_id(url)
    if db.video_exists(conn, video_id):
        return "skipped", []
    meta = youtube.fetch_oembed(video_id)
    return ingest_one(conn, config, video_id, meta["title"], meta["channel"])


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
