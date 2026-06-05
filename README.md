# Mise En Place (mep)

A personal CLI that turns cooking content into a searchable recipe database.
Point it at a YouTube video, a recipe web page, a text file, or pasted text; it
extracts a structured recipe (with Claude, or directly from a page's embedded
recipe data when available) and stores it in local SQLite. Everything stays on
your machine.

## Install

```bash
git clone https://github.com/AveryClapp/MiseEnPlace.git mep && cd mep
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Or simply

```bash
pip install mise-en-place
```

## Setup

```bash
mep init
```

This creates `~/.mep/`, prompts for the keys, and builds the database at
`~/.mep/mep.db`.

- **An LLM key** (required for extraction): set `ANTHROPIC_API_KEY`
  (https://console.anthropic.com/ → API Keys) or `OPENAI_API_KEY`. For OpenAI,
  also install the extra: `pip install 'mise-en-place[openai]'`.
- **YouTube Data API v3 key** (only needed for `--channel` ingestion): see below.
- **The `[tui]` extra** (only for `cook --tui`): `pip install 'mise-en-place[tui]'`.

**Provider selection:** if you set only one of the two LLM keys, that provider
is used automatically. If you set both, it defaults to Anthropic; set
`LLM_PROVIDER=openai` (or choose it at the `mep init` prompt) to pick OpenAI.
An explicit `LLM_PROVIDER` always wins.

Keys are stored in `~/.mep/config.json`. You can also set `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `LLM_PROVIDER`, or `YOUTUBE_API_KEY` as environment variables,
which override the config file. `EXTRACTION_MODEL` overrides the default model
for the chosen provider.

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
mep add https://www.youtube.com/watch?v=VIDEO_ID    # a YouTube video
mep add https://www.seriouseats.com/some-recipe      # a recipe web page
mep add recipe.txt                                   # a local text file
mep add card.png                                     # a photo of a recipe
mep add --image p1.jpg --image p2.jpg               # multi-page spread -> one recipe
mep add --text "2 cups flour, 1 egg, ..."           # pasted recipe text
mep add --channel @JKenjiLopezAlt --limit 10        # latest 10 from a channel
mep add --channel @JKenjiLopezAlt                    # whole channel

mep search "garlic confit"                           # full-text search
mep list                                             # browse, newest first
mep list --tag italian --limit 20                    # filter by tag

mep discover                                         # pick a random recipe
mep discover --type dinner --healthy                 # a random healthy dinner
mep discover --indulgent                             # something to pig out on
mep discover -i chicken -i garlic -n 3               # 3 that use both
mep classify                                         # backfill meal type + health

mep add <url> --pair                                 # ...also suggest + link pairings
mep pair 42                                          # pair one existing recipe
mep pair --all                                       # build the whole pairing graph
mep show 42                                           # full recipe
mep show 42 --servings 8                              # scale ingredient amounts
mep show 42 --macros                                 # estimated nutrition breakdown
mep show 42 --check                                  # flag likely missing steps/gaps
mep set-servings 42 4                                 # record how many it makes
mep set-time 42 "30 minutes"                          # record how long it takes
mep rate 42 5                                          # rate it 1-5 (powers --favorites)
mep note 42 "used less salt, perfect"                 # add a dated cooking note
mep edit 42                                            # fix a recipe by hand ($EDITOR, JSON)
mep export 42                                         # print as Markdown (or -o file.md)
mep export --all -o backup.json                       # back up the whole collection
mep import backup.json                                # restore (skips ones you have)
mep delete 42                                          # remove a recipe (asks first; -f to skip)
mep shopping-list 42 7 13                              # one combined grocery list

mep pantry add eggs milk flour                        # track what you have on hand
mep cook-now                                           # recipes ranked by fewest missing items
mep history                                            # what you've cooked recently

mep plan 42                                          # AI cooking timeline (experimental)
mep plan 42 --servings 8                             # ...scaled to 8 servings
mep plan 42 --with 7                                 # interleave a side into one timeline
mep cook 42                                          # step-by-step walkthrough (experimental)
mep cook 42 --with 7                                 # cook a main + side in one session
mep cook 42 --tui                                    # full-screen view of every pot and pan

mep show 42 --parts                                  # what each ingredient is for
mep clarify 42                                        # name the pots/pans in the steps
mep clarify --all                                     # ...for every recipe
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
vague amounts like "a handful" pass through untouched, and nothing is saved. A
recipe with no recorded serving count is treated as a single serving (the batch
as written), so `--servings 3` simply makes 3× the recipe. Use `mep set-servings
<id> <count>` to record the real serving count when you know it, so scaling maps
to people instead.

`--with <id>` (on `plan` and `cook`, repeatable) merges a side or second dish
into one interleaved timeline, slotting each dish's hands-on prep into the
other's hands-off waits so everything finishes together. Every step is labeled
with its dish and the mise en place is merged. This combined plan is generated
fresh each time (one model call, not cached), and a combined `cook` counts toward
every dish in the session.

`cook --tui` is an optional full-screen version that draws one lane per piece of
cookware (the oven, each pot and pan) and shows what is in each and how long it
has left. Nothing is assumed to be cooking already: a lane only starts counting
when you start that step, and when a timer finishes it rings and holds at
`READY` until you press `a` to acknowledge it, never advancing on its own. It
needs the extra: `pip install 'mise-en-place[tui]'`. The plain `cook` above
needs no extra.

Both `plan` and `cook` are experimental: the timings are AI estimates.

`clarify` rewrites a recipe's stored steps so each names the pot or pan it uses
("In a large skillet, sear..."), for recipes whose source left it vague. Give it
recipe ids or `--all` (one model call each). New recipes already get cookware
named when first added, and `plan`/`cook` show a per-step equipment line.

`show --macros` shows an estimated nutrition breakdown (calories, protein, carbs,
fat) for the whole recipe and per serving. It's computed lazily on first request
(one model call, then cached, so it's free afterward and costs nothing if you
never ask) and is a rough estimate from the ingredients, not exact.

`show --parts` breaks a recipe into its components (marinade, pita, sauce…) so
you can see what each ingredient is for. `adapt` rewrites the recipe around what
you already have: pick the parts you bought or made ahead and it drops the steps
and ingredients needed only to make those (keeping the steps that use them), and
applies any ingredient swaps. It then offers to save the result as a new copy,
overwrite the original, or discard it. `cook --have/--sub` does the same rewrite
in memory for a single cook without saving anything. These are experimental and
use Claude; the rewrite is intentionally light (it shifts and trims the recipe,
it doesn't reinvent it).

Pairing answers "what do I serve with this?" It is opt-in (one extra small
call): `mep add <source> --pair` computes pairings as you ingest, or `mep pair
<id>` / `mep pair --all` does it on demand for recipes you already have. Each
recipe gets a few generic ideas (a side, a drink, a finishing touch) plus links
to recipes already in your collection that go well with it. Those links are
mutual edges in a "goes well with" graph that fills in as you pair more recipes,
and they show up automatically under "Serve with" and "Pairs with" in `mep show`.

`discover` picks a random recipe from your collection, optionally filtered. Use
`--type` (breakfast, lunch, dinner, snack, sweets), `--healthy` (health score
>= 7) or `--indulgent` (<= 4), or `--min-health`/`--max-health` for an exact
range, `-i/--ingredient` (repeatable) to require ingredients you want to use,
`--max-time N` for recipes that cook in N minutes or less, and `--favorites` (or
`--min-rating N`) to stick to recipes you've rated highly. `-n/--count` returns
more than one; with no filters it is completely random. A single pick prints the
full recipe; several print as a list.

`--max-time` also works on `mep list` (e.g. `mep list --max-time 30`). It reads
each recipe's stored cook time (parsing freeform text like "1 hr 30 min" or
"25-35 minutes", where a range uses the upper bound), so recipes with no recorded
cook time are excluded from the results. Use `mep set-time <id> "<time>"` to fill
one in (the extractor never guesses cook time).

Make a recipe yours over time: `mep rate <id> 1-5` records a rating and
`mep note <id> "..."` appends a dated note (both show in `mep show`). Every
`cook` is logged, so `mep history` shows what you've made recently and `show`
notes when you last cooked it. `mep edit <id>` opens the recipe's fields as JSON
in your `$EDITOR` for a precise hand-fix (no model call); saving clears the
derived caches (classification, plan, pairings) since the content changed.

`mep pantry add/remove/list` tracks what you keep on hand, and `mep cook-now`
ranks your recipes by how few ingredients you'd still need to buy ("have
everything!" first), showing the shopping gap for each.

For backup or moving machines, `mep export --all -o backup.json` writes every
recipe (with its rating, notes, and classification) to one JSON file, and
`mep import backup.json` restores them, skipping any whose source you already
have.

To support that, every recipe is given a meal type and a 1-10 health score (10 =
lean and vegetable-forward, 1 = rich and indulgent) by a small model call at
`add` time. They show up in `mep show` and drive `discover`'s filters. Recipes
added before this feature won't have them yet, so run `mep classify` once to
backfill (or `mep classify --all` to redo every recipe); `discover` reminds you
when a type/health filter could be hiding unclassified recipes.

`show --check` makes one model call to flag likely holes in a recipe: a step
that uses an ingredient never listed, a cooking step missing an obvious time or
temperature, an apparent skipped step. It only points at gaps; it never invents
or fills them (anything not in the source text, like a detail shown on screen in
a video, can't be recovered). The result is cached, and an empty result ("no
obvious gaps") is remembered too, so a re-check is free.

Most videos are one recipe, but a video that clearly teaches several independent
dishes ("3 weeknight dinners") is split into separate recipes on `add`, each
with its own id. Sub-preparations that belong to one finished dish (a sauce,
dough, or marinade) stay part of that single recipe and show up under
`show --parts`, not as their own entries.

`export` prints a recipe as a portable Markdown card to stdout, or writes it to
a file with `-o`. `delete` removes a recipe and everything stored with it
(ingredients, steps, tags, and any cached plan, components, macros, gaps, and
classification); it asks first unless you pass `-f`. `shopping-list` takes one or
more recipe ids and makes a single
model call to merge their ingredients into one grocery list, summing compatible
amounts and grouping by aisle. The combined amounts are estimates shown only on
screen; nothing in the database is normalized or changed.

Channel ingestion is idempotent: videos already stored are skipped, so you can
re-run it to pick up only what's new. Non-recipe videos and videos without
transcripts are stored as empty entries (not errors) so they aren't re-fetched.

## Sources

`mep add` takes more than YouTube. Pass it any of:

- **A YouTube URL**: transcript to a recipe (as above).
- **A recipe web page URL**: most recipe sites embed their recipe as schema.org
  data, which `mep` reads directly: accurate, and usually with no extraction call
  at all. Pages without it fall back to extracting from the page text.
- **A local text file** (`mep add recipe.txt`) **or pasted text**
  (`mep add --text "..."`), for recipes from anywhere else.
- **A photo of a recipe** (`mep add card.png`, or `--image` repeated for a
  multi-page spread), read by a vision model. Cookbook pages, recipe cards, and
  screenshots all work; for a YouTube Short whose recipe is on screen rather than
  spoken, screenshot the frame and add it. Supported formats are JPG, PNG, WebP,
  and GIF (convert HEIC first); images over 5 MB should be shrunk.

Each is de-duplicated by a stable id (the video id, the normalized URL, or a hash
of the text or image bytes), so re-adding the same source is a no-op. `mep show`
notes where a recipe came from. Adding a page, text, or image that isn't a recipe
is a clean error, not a stored stub (only channel syncs keep stubs, to avoid
re-fetching duds).

## Cost

Everything runs on your own API key, so you pay the provider directly. The only
cost is the model calls; storage and search are local and free.

- **Adding a recipe** is the main cost: one extraction call (empirically around
  **$0.05** with the default Anthropic model, more for very long videos) plus a
  small classification call per recipe for meal type and health score. A
  `--channel` walk is just this times the number of videos. Web pages that embed
  schema.org recipe data skip the extraction call entirely (only the small
  classification call remains); text, photos, and JSON-LD-less pages cost like a
  video (one extraction/vision call plus classification).
- **On-demand features** (`plan`, `show --parts`, `show --macros`,
  `show --check`, `shopping-list`, `adapt`, pairing via `--pair`/`mep pair`, and
  `cook` with `--have`/`--sub`) each make one additional call when first used, on
  the same order as an extraction or less.
- **Caching keeps it one-and-done.** Plans, components, macros, and gap checks
  are stored after the first request and reused for free; `search`, `list`,
  `show`, and a cached `cook` never call a model at all. Features you never touch
  cost nothing.

Numbers are rough and depend on the provider, model (`EXTRACTION_MODEL`), and
recipe length. OpenAI (`gpt-4o`) lands in a similar range.

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

`source → text or image (YouTube transcript, web page, pasted text, or a photo)
→ an LLM (Claude or OpenAI), or a web page's embedded schema.org recipe → JSON →
SQLite`. Search uses
SQLite FTS5 over dish name, ingredients, and channel. Vague quantities like "a
handful" are stored verbatim; nothing is normalized.

## Develop

```bash
pip install -e '.[dev]'
pytest
```

The test suite is fully offline (no network, no API keys).
