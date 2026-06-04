# CLAUDE.md — mep

Guidance for working in this repo. Design docs live locally under `docs/plans/`
(untracked).

## What this is

A single-user CLI that extracts structured recipes from YouTube videos, recipe
web pages, and pasted text, and stores them in local SQLite at `~/.mep/mep.db`. Python + `click` + stdlib
`sqlite3` (no ORM). No server, no web UI, no accounts.

The command is `mep` (the name `mise` is taken by the unrelated jdx/mise
tool-version manager). The Python package, config dir, and DB are all named `mep`
(`~/.mep/mep.db`); a one-time `migrate_legacy_home` copies a pre-rename `~/.mise`
over on first run.

## Layout

```
mep/
  cli.py         Click commands (init, add, search, show, list, plan, cook, adapt)
  config.py      ~/.mep paths, config load/validate, model id, legacy migration
  errors.py      MepError — user-fixable problems, caught in cli.main()
  llm.py         create_message: shared Claude call with retry/backoff
  db.py          Schema, inserts, FTS5 search, plan/component storage, read queries
  transcript.py  URL -> video_id, transcript fetch (None if unavailable)
  web.py         Recipe web page -> schema.org JSON-LD (or readable text fallback)
  extract.py     Transcript -> Claude -> parsed recipe dict
  youtube.py     oEmbed metadata (keyless) + Data API channel walk
  ingest.py      Orchestration: _store_recipes + add_video/add_channel/add_url/
                 add_text/add_source (dispatch a file, YouTube URL, or web URL)
  plan.py        Recipe -> Claude -> cooking timeline (experimental)
  cook.py        Live step-by-step walkthrough + pure timer helpers
  scale.py       Best-effort serving-size quantity scaling (pure)
  components.py  Recipe -> Claude -> component breakdown (for adapt)
  adapt.py       Recipe -> Claude -> rewrite around what you have (experimental)
  nutrition.py   Recipe -> Claude -> macro estimate (lazy, cached)
  gaps.py        Recipe -> Claude -> likely missing-step/gap flags (lazy, cached)
  classify.py    Recipe -> Claude -> meal_type + 1-10 health_score (for discover)
  shopping.py    Recipes -> Claude -> one combined grocery list (display-only)
tests/           Offline unit tests (no network, no keys)
docs/plans/      Design docs
```

## Conventions

- **Errors:** raise `MepError` for anything the user can fix (missing key, bad
  URL, unknown channel). It is caught in `cli.main()` and printed cleanly with
  exit 1. Let real bugs raise a normal traceback — don't wrap them.
- **`video_id` is the idempotency key** (`UNIQUE` in `recipes`), now holding a
  source-appropriate stable id: a YouTube id, a `web.canonical_url` (normalized
  URL), or `text:<sha256[:16]>`. Adds check existence first; re-adding is a no-op
  skip. `source_type` ('youtube' / 'web' / 'text') records the origin; legacy
  rows backfill to 'youtube' in `_migrate`.
- **Sources share one tail.** `ingest._store_recipes` does extract -> classify ->
  insert for every source; the `add_*` functions only differ in how they fetch
  text and an id. `add_url` prefers schema.org JSON-LD (`web.py`, no extraction
  call) and falls back to the LLM over page text. Web/text adds raise on a
  non-recipe (no stub); only YouTube channel syncs keep stubs to avoid re-fetch.
- **A video may yield multiple recipes.** Extraction returns a list (conservative
  split: only genuinely independent dishes, never sub-preparations). The first
  recipe keeps the real `video_id`; extras get a `#N` suffix. The base `video_id`
  stays the idempotency anchor, so the existence check still covers the whole
  video. `ingest_one`/`add_video`/`add_channel` return a list of `(recipe_id,
  dish_name)` results.
- **Never normalize quantities or units.** Store "a handful" / "to taste"
  exactly as Claude returns them. Quantity/cook_time/servings/difficulty are
  TEXT and nullable.
- **Non-recipe and no-transcript videos are stored as empty stubs**
  (`dish_name = NULL`, empty children), never errors. This stops channel syncs
  from re-fetching duds.
- **Classification (`meal_type`, `health_score`) powers `discover`.** Each real
  recipe is classified by a small `classify.py` call at ingest; stubs are
  skipped. `mep classify` backfills recipes with `meal_type IS NULL` (or `--all`
  to redo). `db.discover` filters on these columns, so unclassified recipes are
  naturally excluded from type/health filters. Cleared on overwrite alongside
  the macro/gap caches.
- **FTS:** `recipe_fts` is a contentless FTS5 table keyed by `rowid = recipes.id`,
  populated inside the same transaction as the recipe insert. If you add a
  searchable field, update both the schema and `insert_recipe`. Contentless rows
  don't cascade or UPDATE: removing one needs the special `'delete'` command with
  the originally-indexed values (see `delete_recipe` / `replace_recipe_content`).
- **Two YouTube paths:** oEmbed (keyless, single-video metadata) vs Data API
  (needs `YOUTUBE_API_KEY`, channel ingestion only). Keep single-video adds
  working without a YouTube key.
- **Throttle:** 0.5s between transcript fetches during channel ingest. Don't
  remove it — it avoids rate limiting.
- **Config never hard-fails on load.** `load_config` returns whatever keys are
  available; `require()` enforces a specific key only when an operation actually
  needs it. So cached `plan`/`cook`, plus `search`/`list`/`show`, work with no
  keys at all. Don't reintroduce a global "no config" error.
- **Plans are cached** in `plan_steps` (one ordered set per recipe). `plan`
  generates on first use, reuses after; `plan --regenerate` and `save_plan`
  overwrite cleanly. `cook` never calls Claude if a plan already exists.

## Extraction

The system prompt in `extract.py` owns the JSON contract: a `{"recipes": [...]}`
object. `_parse_json` is deliberately forgiving (strips fences, slices to
outermost braces); `_parse_recipes` then pulls the list and keeps only entries
with a `dish_name` (a null `dish_name` is the model's "not a recipe" signal, so
that filters to an empty list -> one empty stub). If you change the recipe shape,
update the prompt, `_parse_recipes` expectations, and `db.insert_recipe`
together. Truncated model responses (hit `max_tokens`) raise a clear `MepError`
from `llm.py` rather than failing JSON parsing.

## LLM provider

All model calls (extract, plan, components, adapt) go through `llm.complete(config,
system=..., user=..., max_tokens=...)`, which returns text and retries transient
errors. `config` selects the backend: `LLM_PROVIDER` is `anthropic` (default) or
`openai`; `require_api_key`/`model` resolve the right key and default model
(`claude-sonnet-4-20250514` / `gpt-4o`, overridable via `EXTRACTION_MODEL`). The
`openai` SDK is an optional extra, imported lazily. Pass `config` to these
functions — don't thread `api_key`/`model` through.

## Testing

`pytest` must stay fully offline — mock or avoid network and API calls. Tests
set `MEP_HOME` to a temp dir before importing mep modules so they never touch
a real `~/.mep`. Cover pure logic: id parsing, JSON parsing, DB round-trip,
FTS search.

## Releasing

Published on PyPI as `mise-en-place` (import package and command stay `mep`).
When changes warrant a release, bump `version` in `pyproject.toml` using
**semver** (MAJOR.MINOR.PATCH: breaking / feature / fix), then tag and push:

```
git tag vX.Y.Z && git push origin vX.Y.Z
```

The tag triggers `.github/workflows/release.yml`, which builds and publishes via
PyPI trusted publishing (OIDC, no tokens). The git tag must match the
`pyproject.toml` version. Don't reuse or move a published version — bump instead.

## House rules

- Keep it minimal. No speculative features, no abstractions for single-use code.
- Surgical changes: every changed line should trace to the request.
- Match existing style. Don't refactor unrelated code.
- No co-authored commit trailers. No em dashes in user-facing writing.
