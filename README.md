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
mep add --channel @JKenjiLopezAlt --limit 10        # latest 10 from a channel
mep add --channel @JKenjiLopezAlt                    # whole channel

mep search "garlic confit"                           # full-text search
mep list                                             # browse, newest first
mep list --tag italian --limit 20                    # filter by tag
mep show 42                                           # full recipe
mep show 42 --servings 8                              # scale ingredient amounts

mep plan 42                                          # AI cooking timeline (experimental)
mep plan 42 --servings 8                             # ...scaled to 8 servings
mep cook 42                                          # step-by-step walkthrough (experimental)

mep show 42 --parts                                  # what each ingredient is for
mep adapt 42                                         # rewrite around what you have (interactive)
mep adapt 42 --have pita --sub "yogurt=sour cream"   # ...or state it directly
mep cook 42 --have pita                              # adapt just for this cook
```

`plan` makes one Claude call to reorder a recipe's steps into an efficient
timeline and caches it. Each step is tagged hands-on or hands-off, with the
ingredients and equipment it uses, a named timer for waits, and a "prep this
during the wait" hint. The summary shows realistic wall-clock time (hands-off
waits run in the background, not added end to end). Re-run `plan --regenerate`
to rebuild.

`cook` walks that timeline live: it opens with a mise en place gather + equipment
list, then one step at a time. On a hands-off step, pressing Enter starts a named
background timer that keeps counting while you move on to the next step (like a
real kitchen timer); it rings when done. It also nudges you to preheat the oven a
couple steps ahead. Ctrl-C stops cleanly and reports any timers still running.

`--servings N` (on `show`, `plan`, `cook`) scales ingredient amounts to N
servings. It is best-effort and display-only: only leading quantities are scaled,
vague amounts like "a handful" pass through untouched, and nothing is saved. If
the recipe's serving count can't be read, amounts are shown unscaled with a note.

Both `plan` and `cook` are experimental: the timings are AI estimates.

`show --parts` breaks a recipe into its components (marinade, pita, sauce…) so
you can see what each ingredient is for. `adapt` rewrites the recipe around what
you already have: pick the parts you bought or made ahead and it drops the steps
and ingredients needed only to make those (keeping the steps that use them), and
applies any ingredient swaps. It then offers to save the result as a new copy,
overwrite the original, or discard it. `cook --have/--sub` does the same rewrite
in memory for a single cook without saving anything. These are experimental and
use Claude; the rewrite is intentionally light (it shifts and trims the recipe,
it doesn't reinvent it).

Channel ingestion is idempotent: videos already stored are skipped, so you can
re-run it to pick up only what's new. Non-recipe videos and videos without
transcripts are stored as empty entries (not errors) so they aren't re-fetched.

## Channels to try

Recipe-forward channels that work well (most videos are real walkthroughs with
transcripts). Single-video adds need no key; the `--channel` walk needs a YouTube
Data API key (see above).

| Channel | Handle |
| --- | --- |
| Babish Culinary Universe | `@babishculinaryuniverse` |
| J. Kenji López-Alt | `@JKenjiLopezAlt` |
| Joshua Weissman | `@joshuaweissman` |
| Adam Ragusea | `@aragusea` |
| Ethan Chlebowski | `@EthanChlebowski` |
| Brian Lagerstrom | `@brianlagerstrom` |
| Food Wishes (Chef John) | `@foodwishes` |

```bash
mep add https://www.youtube.com/watch?v=iErqWGwso7o   # a single Babish video, no key needed
mep add --channel @aragusea --limit 5                 # latest 5 from Adam Ragusea
```

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
