# Adapt a recipe to what you have — Design

**Date:** 2026-06-03
**Status:** Implemented (experimental)

## Purpose

Cook a stored recipe based on what you already have. Two moves:

- **Skip a sub-recipe you already own.** If you bought pita, drop the steps and
  ingredients that exist only to make pita; keep the steps that *use* it.
- **Substitute an ingredient.** "Use sour cream instead of yogurt."

The rewrite is deliberately light: the recipe **shifts and loses parts**, the AI
does not reinvent the dish. You can save the result as a new copy, overwrite the
original, or just cook it once.

## Components — the backbone

Recipes are stored flat (ingredient list + step list) with no structure. One AI
pass groups a recipe into **components**:

```json
{"components": [
  {"name": str, "purpose": str,
   "ingredients": [str, ...], "make_steps": [int, ...]}
]}
```

- `name` — short label ("Pita bread", "Marinade").
- `purpose` — one line: what it is / what it's for.
- `ingredients` — the ingredient lines this component consumes (with amounts).
- `make_steps` — the step numbers that **produce** it, as opposed to steps that
  merely use it later.

Cached per recipe in `recipe_components` (same pattern as `plan_steps`), so the
analysis runs once. It powers both the read-only breakdown and the "what do you
have" picker.

## Adaptation

Given a recipe plus the components you have and any substitutions, one AI call
returns the adapted recipe in the **same JSON shape as extraction** (so it flows
straight back through `insert_recipe` and every existing `show`/`plan`/`cook`):

```json
{"dish_name", "cook_time", "servings", "difficulty",
 "ingredients": [...], "steps": [...], "tags": [...]}
```

Prompt rules (conservative):

- Remove only what's needed to **make** a thing the cook already has; keep steps
  that use it, lightly reworded to refer to the ready-made item.
- For an ingredient shared across parts, drop only the cut part's share; remove
  it entirely if it was used nowhere else.
- Apply substitutions with minimal wording/quantity changes.
- Otherwise leave the recipe alone. Keep vague quantities verbatim.

Both `components.py` and `adapt.py` reuse `extract._parse_json` (forgiving) and
`llm.create_message` (retry/backoff).

## Commands

```
mep show 1 --parts                                  # read-only breakdown
mep adapt 1 [--have a,b] [--sub "x=y"] [-i]          # rewrite + save/overwrite/discard
mep cook 1 [--have a,b] [--sub "x=y"]                # adapt in memory, cook once
```

- `--have` cuts **components**; `--sub` swaps **ingredients** (repeatable as a
  comma list). With no flags, `adapt` runs interactively.
- `cook --have/--sub` adapts in memory only. The original's cached plan is for
  the un-adapted recipe, so an ephemeral plan is generated for the adapted
  version and **not** cached.

### Interactive flow (UX-first)

One screen, batched input, single keystrokes — no per-ingredient interrogation:

```
Shawarma — what do you already have?

  1  Pita bread     flour, water, yeast, oil      · 4 steps
  2  Marinade       yogurt, oil, garlic, spices   · 2 steps
  ...

Have any already? numbers, e.g. 1,4  (Enter = none) › 1
Any swaps? ingredient=replacement     (Enter = none) › yogurt=sour cream

  have:  Pita bread
  swaps: yogurt → sour cream
  Shawarma (adapted)   26 → 21 ingredients · 16 → 12 steps

Save?  [c] copy   [o] overwrite   [d] discard ›
```

Colors match `cook` (component numbers cyan, cuts red, swaps yellow, result
bold). The preview shows before→after counts (always accurate, both modes).

## Saving

- **copy** → `insert_recipe` with a unique synthetic `video_id`
  (`{orig}~adapted`, `~adapted2`, … so it stays idempotent-safe), title suffixed
  "(adapted)", same channel/url. First-class: shows in `list`/`search`, can be
  planned/cooked/re-adapted.
- **overwrite** → `replace_recipe_content`: swap the original's ingredients,
  steps, tags, and recipe fields; refresh FTS; clear its cached `plan_steps` and
  `recipe_components` (they're now stale).
- **discard** → print only.

## Storage

```sql
CREATE TABLE recipe_components (
  id, recipe_id REFERENCES recipes(id) ON DELETE CASCADE,
  position, name, purpose, ingredients_json, make_steps_json
);
```

Created by `init_db` (IF NOT EXISTS — no ALTER needed). `save_components`
replaces the set; `get_components` returns it ordered or `[]`.

## Modules

- `mise/components.py` — `analyze_components`, `_normalize_components`.
- `mise/adapt.py` — `adapt_recipe`, pure `parse_selection`/`parse_subs`.
- `mise/db.py` — `recipe_components` schema, `save_components`/`get_components`,
  `replace_recipe_content`, `next_adapted_video_id`.
- `mise/cli.py` — `adapt` command, `show --parts`, `cook --have/--sub`.

## Testing (offline)

- `parse_selection` (numbers, ranges of input like "1,4", junk ignored,
  out-of-range dropped) and `parse_subs` ("x=y, a=b").
- `save_components`/`get_components` round-trip + cascade delete.
- `replace_recipe_content` swaps content, refreshes FTS, clears plan/components.
- `next_adapted_video_id` returns a free, unique id.
- AI calls (`analyze_components`, `adapt_recipe`) and the interactive loop are
  not unit-tested (network / TTY).

## Scope guard (YAGNI)

No automatic pantry, no nutrition math, no merging multiple recipes, no undo
beyond keeping the original. The AI does light surgery only. Experimental.
