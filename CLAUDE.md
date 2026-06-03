# CLAUDE.md — mise

Guidance for working in this repo. Read alongside the design doc in
`docs/plans/2026-06-03-mise-design.md`.

## What this is

A single-user CLI that extracts structured recipes from YouTube cooking videos
and stores them in local SQLite at `~/.mise/mise.db`. Python + `click` + stdlib
`sqlite3` (no ORM). No server, no web UI, no accounts.

The command is `mep` (the name `mise` is taken by the unrelated jdx/mise
tool-version manager). The Python package, config dir, and DB stay named `mise`.

## Layout

```
mise/
  cli.py         Click commands (init, add, search, show, list) + error boundary
  config.py      ~/.mise paths, config load/validate, extraction model id
  errors.py      MiseError — user-fixable problems, caught in cli.main()
  db.py          Schema, inserts, FTS5 search, read queries
  transcript.py  URL -> video_id, transcript fetch (None if unavailable)
  extract.py     Transcript -> Claude -> parsed recipe dict
  youtube.py     oEmbed metadata (keyless) + Data API channel walk
  ingest.py      Orchestration: ingest_one / add_video / add_channel
tests/           Offline unit tests (no network, no keys)
docs/plans/      Design doc
```

## Conventions

- **Errors:** raise `MiseError` for anything the user can fix (missing key, bad
  URL, unknown channel). It is caught in `cli.main()` and printed cleanly with
  exit 1. Let real bugs raise a normal traceback — don't wrap them.
- **`video_id` is the idempotency key** (`UNIQUE` in `recipes`). Adds check
  existence first; re-adding is a no-op skip.
- **Never normalize quantities or units.** Store "a handful" / "to taste"
  exactly as Claude returns them. Quantity/cook_time/servings/difficulty are
  TEXT and nullable.
- **Non-recipe and no-transcript videos are stored as empty stubs**
  (`dish_name = NULL`, empty children), never errors. This stops channel syncs
  from re-fetching duds.
- **FTS:** `recipe_fts` is a contentless FTS5 table keyed by `rowid = recipes.id`,
  populated inside the same transaction as the recipe insert. If you add a
  searchable field, update both the schema and `insert_recipe`.
- **Two YouTube paths:** oEmbed (keyless, single-video metadata) vs Data API
  (needs `YOUTUBE_API_KEY`, channel ingestion only). Keep single-video adds
  working without a YouTube key.
- **Throttle:** 0.5s between transcript fetches during channel ingest. Don't
  remove it — it avoids rate limiting.

## Extraction

Model `claude-sonnet-4-20250514` (override via config `EXTRACTION_MODEL`). The
system prompt in `extract.py` owns the JSON contract; `_parse_json` is
deliberately forgiving (strips fences, slices to outermost braces). If you
change the recipe shape, update the prompt, `_parse_json` expectations, and
`db.insert_recipe` together.

## Testing

`pytest` must stay fully offline — mock or avoid network and API calls. Tests
set `MISE_HOME` to a temp dir before importing mise modules so they never touch
a real `~/.mise`. Cover pure logic: id parsing, JSON parsing, DB round-trip,
FTS search.

## House rules

- Keep it minimal. No speculative features, no abstractions for single-use code.
- Surgical changes: every changed line should trace to the request.
- Match existing style. Don't refactor unrelated code.
- No co-authored commit trailers. No em dashes in user-facing writing.
