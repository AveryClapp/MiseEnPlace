# mise — Design & Implementation Plan

**Date:** 2026-06-03
**Status:** Implemented (v0.1.0)

## 1. Purpose

`mise` is a personal CLI that turns YouTube cooking videos into a searchable,
structured recipe database. It pulls a video's transcript, sends it to Claude
for structured extraction, and stores the result in local SQLite. No web UI, no
server, no accounts — one person, one machine, one database at `~/.mise/mise.db`.

Named after *mise en place*: everything in its place before you cook.

## 2. Scope

In scope:
- Ingest a single video by URL.
- Ingest a whole channel (or the latest N videos) by handle.
- Full-text search across dish name, ingredients, and channel.
- Show one recipe; list/browse recipes with optional tag filter.
- Local config + DB bootstrap via `mep init`.

Explicitly out of scope (YAGNI):
- Editing recipes by hand, shopping lists, meal planning, exporting.
- Any non-YouTube source.
- Normalizing or converting quantities/units.
- Multi-user, sync, or remote storage.

## 3. Architecture

Single Python package, `click` CLI, SQLite (stdlib `sqlite3`, no ORM).

```
mise/
  cli.py          Click commands: init, add, search, show, list
  config.py       ~/.mise paths, config load/validate, model id
  errors.py       MiseError (user-facing, caught at CLI boundary)
  db.py           Schema, connection, inserts, FTS5 search, queries
  transcript.py   URL -> video_id, fetch transcript text
  extract.py      Transcript -> Claude -> parsed recipe dict
  youtube.py      oEmbed metadata + YouTube Data API channel walk
  ingest.py       Orchestration: add_video / add_channel
```

Data flow for a single add:

```
url -> extract_video_id -> oEmbed(title, channel)
    -> fetch_transcript -> extract_recipe (Claude JSON)
    -> db.insert_recipe (recipes + ingredients + steps + tags + FTS row)
```

Channel add is the same inner step looped over the uploads playlist, with a
0.5s sleep between transcript fetches and a DB existence check up front so reruns
are idempotent.

## 4. Data Model

SQLite, foreign keys on, cascade delete from `recipes`.

- `recipes(id, video_id UNIQUE, title, channel, url, dish_name, cook_time,
  servings, difficulty, raw_transcript, created_at)`
- `ingredients(id, recipe_id, name, quantity, unit, prep)`
- `steps(id, recipe_id, step_number, instruction)`
- `tags(id, recipe_id, tag)`
- `recipe_fts` — FTS5 contentless virtual table `(dish_name, channel,
  ingredients)` keyed by `rowid = recipes.id`.

Design notes:
- `video_id` is the idempotency key (`UNIQUE`). Re-adding is a no-op skip.
- Quantity/cook_time/servings/difficulty are **TEXT and nullable**. Transcripts
  say "a handful" and "45-ish minutes"; we store verbatim, never normalize.
- A non-recipe video is stored with `dish_name = NULL` and empty
  ingredients/steps/tags. That is a success, not an error — it stops us
  re-fetching the same dud on the next channel sync.
- Ingredients are flattened into the FTS row as a single space-joined string of
  ingredient names at insert time.

## 5. Claude Extraction

- Model: `claude-sonnet-4-20250514` (overridable via config `EXTRACTION_MODEL`).
- One message, `max_tokens` ~2000, system prompt fixes the JSON contract.
- The prompt explicitly handles: unpunctuated auto-generated text; vague
  quantities kept as-is; pick the **primary** recipe when several appear;
  emit `dish_name: null` for non-recipe videos instead of hallucinating.
- Response is parsed defensively: strip code fences, slice first `{` to last `}`,
  `json.loads`. Missing keys default to null/empty via `.get`.

## 6. Channel Ingestion

- Resolve `@handle` -> channel via Data API `channels.list(forHandle=...)`,
  read `contentDetails.relatedPlaylists.uploads` and the channel title.
- Page `playlistItems.list` (50/page) until `--limit` or no `nextPageToken`.
- For each video: skip if `video_id` already in DB; else run the inner ingest
  step; `sleep(0.5)` between transcript requests.

Single-video adds use the public **oEmbed** endpoint for title/channel, so they
need only the Anthropic key — not a YouTube Data API key.

## 7. CLI Surface

The command is `mep` (the name `mise` is taken by the unrelated jdx/mise
tool-version manager). The Python package and DB are still named `mise`.

```
mep init                                    prompt for keys, create ~/.mise + DB
mep add <url>                               ingest one video
mep add --channel <handle> [--limit N]      ingest channel / latest N
mep search <query>                          FTS5 search
mep show <recipe_id>                        full recipe, formatted
mep list [--tag <tag>] [--limit N]          browse (newest first)
```

`MiseError` is caught at the CLI boundary and printed as a clean message with a
non-zero exit; everything else propagates as a normal traceback (a bug).

## 8. Task Breakdown

1. **Package skeleton** → `pip install -e .` exposes `mise --help`.
2. **config.py / errors.py** → paths resolve, missing config raises MiseError.
3. **db.py** → `init_db()` creates all tables + FTS; round-trip insert/get works.
4. **transcript.py** → URL forms (`watch?v=`, `youtu.be`, `/shorts/`, bare id)
   parse; missing transcript returns `None` not an exception.
5. **extract.py** → transcript in, valid recipe dict out; fenced/garbled JSON
   still parses; non-recipe yields `dish_name=None`.
6. **youtube.py** → oEmbed metadata; handle resolves to uploads playlist;
   paginator respects `--limit`.
7. **ingest.py** → add_video/add_channel; idempotent skip; 0.5s throttle.
8. **cli.py** → wire all five commands + `init`; clean error reporting.
9. **README + CLAUDE.md** → setup incl. getting a YouTube Data API v3 key.
10. **tests** → pure logic (id parsing, JSON parsing, schema round-trip) without
    network or API keys.

## 9. Verification

- `mep init` writes `~/.mise/config.json` and creates `~/.mise/mise.db`.
- `pytest` passes for the offline units in §8.10.
- Manual smoke (needs keys): `mep add <known cooking video>` then
  `mep show 1`, `mep search <ingredient>`, `mep list`.
- Idempotency: re-running `mep add --channel <handle>` reports all skipped.
