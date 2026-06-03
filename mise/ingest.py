"""Orchestration: pull transcript, extract with Claude, store.

ingest_one is the single unit of work. add_video wraps it with oEmbed metadata
for a one-off URL; add_channel loops it over a channel's uploads, skipping
videos already stored and throttling transcript requests.
"""

import time

from . import db, youtube
from .config import model, require
from .extract import extract_recipe
from .transcript import extract_video_id, fetch_transcript

THROTTLE_SECONDS = 0.5

# Stored when a video has no transcript or is not a recipe: a valid, empty row.
_EMPTY = {"dish_name": None, "ingredients": [], "steps": [], "tags": []}


def ingest_one(conn, config, video_id, title, channel) -> tuple[str, int, str | None]:
    """Ingest a single video. Returns (status, recipe_id, dish_name).
    status is one of: 'added', 'no_transcript'."""
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
        return "no_transcript", recipe_id, None

    extracted = extract_recipe(
        transcript,
        title=title,
        api_key=require(config, "ANTHROPIC_API_KEY"),
        model=model(config),
    )
    recipe_id = db.insert_recipe(
        conn,
        video_id=video_id,
        title=title,
        channel=channel,
        url=url,
        raw_transcript=transcript,
        extracted=extracted,
    )
    return "added", recipe_id, extracted.get("dish_name")


def add_video(conn, config, url) -> tuple[str, int | None, str | None]:
    """Ingest one video by URL. Returns (status, recipe_id, dish_name);
    status may also be 'skipped' when the video is already stored."""
    video_id = extract_video_id(url)
    if db.video_exists(conn, video_id):
        return "skipped", None, None
    meta = youtube.fetch_oembed(video_id)
    return ingest_one(conn, config, video_id, meta["title"], meta["channel"])


def add_channel(conn, config, handle, limit=None):
    """Generator of (video_id, title, status, dish_name) per video processed,
    so the CLI can report progress live. Idempotent and throttled."""
    api_key = require(config, "YOUTUBE_API_KEY")
    uploads, channel_title = youtube.resolve_channel(api_key, handle)
    first = True
    for video_id, title in youtube.iter_playlist_videos(api_key, uploads, limit):
        if db.video_exists(conn, video_id):
            yield video_id, title, "skipped", None
            continue
        if not first:
            time.sleep(THROTTLE_SECONDS)
        first = False
        status, _id, dish = ingest_one(conn, config, video_id, title, channel_title)
        yield video_id, title, status, dish
