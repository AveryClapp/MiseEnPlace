# mise

A personal CLI that turns YouTube cooking videos into a searchable recipe
database. It pulls a video's transcript, extracts a structured recipe with
Claude, and stores it in local SQLite. Everything stays on your machine.

## Install

```bash
git clone <this-repo> mise && cd mise
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

The command is named `mep` (the name `mise` is taken by the unrelated
[jdx/mise](https://mise.jdx.dev/) tool-version manager). The Python package is
still `mise`.

## Setup

```bash
mep init
```

This creates `~/.mise/`, prompts for two API keys, and builds the database at
`~/.mise/mise.db`.

- **Anthropic API key** (required): https://console.anthropic.com/ → API Keys.
- **YouTube Data API v3 key** (only needed for `--channel` ingestion): see below.

Keys are stored in `~/.mise/config.json`. You can also set `ANTHROPIC_API_KEY`
or `YOUTUBE_API_KEY` as environment variables, which override the config file.

### Getting a YouTube Data API v3 key

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project (top bar → project dropdown → **New Project**).
3. In the search bar, open **YouTube Data API v3** and click **Enable**.
4. Go to **APIs & Services → Credentials → Create Credentials → API key**.
5. Copy the key. (Optional: **Edit API key → Restrict key → YouTube Data API v3**.)

Single-video adds use YouTube's public oEmbed endpoint and do **not** need this
key. It is only required to walk a channel's uploads.

## Usage

```bash
mep add https://www.youtube.com/watch?v=VIDEO_ID    # one video
mep add --channel @kenjilopezalt --limit 10         # latest 10 from a channel
mep add --channel @kenjilopezalt                     # whole channel

mep search "garlic confit"                           # full-text search
mep list                                             # browse, newest first
mep list --tag italian --limit 20                    # filter by tag
mep show 42                                           # full recipe
```

Channel ingestion is idempotent: videos already stored are skipped, so you can
re-run it to pick up only what's new. Non-recipe videos and videos without
transcripts are stored as empty entries (not errors) so they aren't re-fetched.

## How it works

`url → transcript (youtube-transcript-api) → Claude (claude-sonnet-4-20250514)
→ JSON → SQLite`. Search uses SQLite FTS5 over dish name, ingredients, and
channel. Vague quantities like "a handful" are stored verbatim — nothing is
normalized. See `docs/plans/` for the full design.

## Develop

```bash
pip install -e '.[dev]'
pytest
```

The test suite is fully offline (no network, no API keys).
