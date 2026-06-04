"""YouTube metadata helpers.

Two distinct paths:
- oEmbed: public, keyless. Used to get title + channel for a single video add.
- Data API v3: needs YOUTUBE_API_KEY. Used to resolve a channel handle to its
  uploads playlist and to page through that playlist for channel ingestion.
"""

import json
import urllib.error
import urllib.request

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .errors import MepError


def _api_error(exc: HttpError) -> str:
    reason = getattr(exc, "reason", None) or str(exc)
    return (
        f"YouTube Data API error: {reason}. Check that YOUTUBE_API_KEY is valid, "
        "that the YouTube Data API v3 is enabled for it, and that you are within "
        "your daily quota."
    )


def fetch_oembed(video_id: str) -> dict:
    """Return {'title', 'channel'} for a video without an API key."""
    url = (
        "https://www.youtube.com/oembed?format=json&url="
        f"https://www.youtube.com/watch?v={video_id}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        return {"title": None, "channel": None}
    return {"title": data.get("title"), "channel": data.get("author_name")}


def _client(api_key: str):
    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


def resolve_channel(api_key: str, handle: str) -> tuple[str, str]:
    """Resolve an @handle to (uploads_playlist_id, channel_title)."""
    handle = handle.lstrip("@")
    yt = _client(api_key)
    try:
        resp = (
            yt.channels()
            .list(part="contentDetails,snippet", forHandle=handle)
            .execute()
        )
    except HttpError as exc:
        raise MepError(_api_error(exc))
    items = resp.get("items")
    if not items:
        raise MepError(f"Channel not found: @{handle}")
    channel = items[0]
    uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
    title = channel["snippet"]["title"]
    return uploads, title


def iter_playlist_videos(api_key: str, playlist_id: str, limit: int | None = None):
    """Yield (video_id, title) for each video in a playlist, newest first."""
    yt = _client(api_key)
    token = None
    count = 0
    while True:
        try:
            resp = (
                yt.playlistItems()
                .list(
                    part="contentDetails,snippet",
                    playlistId=playlist_id,
                    maxResults=50,
                    pageToken=token,
                )
                .execute()
            )
        except HttpError as exc:
            raise MepError(_api_error(exc))
        for item in resp.get("items", []):
            yield (
                item["contentDetails"]["videoId"],
                item["snippet"].get("title"),
            )
            count += 1
            if limit and count >= limit:
                return
        token = resp.get("nextPageToken")
        if not token:
            return
