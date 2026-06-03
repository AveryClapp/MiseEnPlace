"""Transcript extraction via youtube-transcript-api.

Parses a video id out of the common YouTube URL shapes, then fetches and
flattens the transcript to a single string. A video with transcripts disabled
or none available returns None (not an error) so callers can store it as a
non-recipe stub instead of crashing a channel sync.
"""

import re

from youtube_transcript_api import YouTubeTranscriptApi

from .errors import MiseError

try:  # exception names differ across library versions
    from youtube_transcript_api import (  # type: ignore
        NoTranscriptFound,
        TranscriptsDisabled,
    )

    _NO_TRANSCRIPT = (TranscriptsDisabled, NoTranscriptFound)
except ImportError:  # pragma: no cover - depends on installed version
    _NO_TRANSCRIPT = ()

_ID_PATTERN = re.compile(
    r"(?:v=|/shorts/|youtu\.be/|/embed/|/v/)([0-9A-Za-z_-]{11})"
)
_BARE_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")


def extract_video_id(url: str) -> str:
    url = url.strip()
    if _BARE_ID.match(url):
        return url
    match = _ID_PATTERN.search(url)
    if not match:
        raise MiseError(f"Could not find a YouTube video id in: {url}")
    return match.group(1)


def fetch_transcript(video_id: str) -> str | None:
    """Return the full transcript text, or None if unavailable."""
    try:
        segments = _fetch_segments(video_id)
    except _NO_TRANSCRIPT:
        return None
    except Exception as exc:  # noqa: BLE001 - surface as a clean user error
        message = str(exc).lower()
        if "transcript" in message or "subtitles" in message:
            return None
        raise MiseError(f"Failed to fetch transcript for {video_id}: {exc}")

    text = " ".join(seg["text"] for seg in segments if seg.get("text")).strip()
    return text or None


def _fetch_segments(video_id: str) -> list[dict]:
    """Bridge the old static API and the newer instance API."""
    if hasattr(YouTubeTranscriptApi, "get_transcript"):
        return YouTubeTranscriptApi.get_transcript(video_id)
    fetched = YouTubeTranscriptApi().fetch(video_id)  # type: ignore[call-arg]
    if hasattr(fetched, "to_raw_data"):
        return fetched.to_raw_data()
    return list(fetched)
