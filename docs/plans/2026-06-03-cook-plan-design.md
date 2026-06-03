# Cook plan + cook-along — Design

**Date:** 2026-06-03
**Status:** Implemented (experimental)

## Purpose

Make actually cooking a stored recipe seamless. Two commands:

- `mep plan <id>` — AI-generated, cached cooking timeline for one recipe.
- `mep cook <id>` — a live walkthrough of that timeline with real background
  timers, mise en place, and per-step ingredient/equipment/preheat cues.

Experimental: timings are AI estimates, not ground truth.

## Generation

We store steps but no durations, so `plan` makes one Claude call. Input: the
recipe's dish name, ingredient lines (with amounts), and ordered steps. Output
JSON:

```json
{"tasks": [
  {"instruction": str, "duration_minutes": number,
   "mode": "active" | "passive", "overlap_hint": str | null,
   "ingredients": [str, ...], "equipment": [str, ...],
   "timer_label": str | null}
]}
```

- `mode`: `active` = hands-on; `passive` = hands-off wait (marinate, bake, rest).
- Claude **reorders** tasks to front-load prep and slot hands-on work into
  hands-off waits, so you are never idle when there is prep to do.
- `overlap_hint`: for a passive wait, a short "do this now" drawn from later
  steps; null otherwise.
- `ingredients`: the specific ingredients that step uses, amounts included.
- `equipment`: tools/appliances the step needs (drives the equipment list and
  the preheat look-ahead).
- `timer_label`: a 1-3 word name for the kitchen timer a passive task starts.

The response is validated and normalized (`_normalize_tasks`): empty
instructions and non-dict entries are dropped, `mode` defaults to `active`,
`duration_minutes` is coerced to a float ≥ 0, and string/list fields are
cleaned. The result is cached so `cook` and re-display reuse it. ~1 to 2 cents
per plan, once per recipe. Same defensive JSON parsing as extraction, plus the
shared retry wrapper in `mise/llm.py` (exponential backoff on rate-limit /
timeout / connection / 5xx errors).

## Storage

```sql
CREATE TABLE plan_steps (
  id, recipe_id REFERENCES recipes(id) ON DELETE CASCADE,
  position, instruction, duration_minutes, mode, overlap_hint,
  ingredients_json, equipment_json, timer_label
);
```

`ingredients`/`equipment` are stored as JSON arrays. The three enrichment
columns were added after the first release; `init_db` runs a non-destructive
`_migrate` (PRAGMA `table_info` + `ALTER TABLE ADD COLUMN`) so existing
databases pick them up. `save_plan` replaces any existing rows for the recipe
(regenerate is a clean overwrite). `get_plan` returns the ordered tasks with
JSON fields parsed back to lists, or `[]` if none.

## Execution model — hybrid concurrent timers

The plan is an **ordered active spine**: you advance steps manually (Enter).
The reordering is what creates efficiency. On top of that, reaching a passive
step starts a **named background timer** that keeps counting while you move on
to the next step, exactly like setting a kitchen timer and getting back to
prep. Running timers show in the live status line and ring the terminal bell
plus a green banner when they finish.

This means time is not a simple sum. `estimate_wallclock_minutes` models it: the
active spine runs sequentially while passive timers overlap, so wall-clock is
`max(active spine, the latest passive tail)`. The summary reports hands-on time
(sum of active durations) and that realistic total.

## Servings scaling

`plan`, `cook`, and `show` take `--servings N`. Scaling is best-effort
(`mise/scale.py`): only the **leading amount** of a quantity string is scaled
("200", "1 1/2", both ends of "3-4"); embedded numbers like the "14" in
"1 (14 oz can)" are left alone, and vague amounts ("a handful", "to taste") pass
through untouched. Fractions are kept kitchen-friendly. The base count comes
from the recipe's `servings` text; if it can't be read, amounts are shown
unscaled with a note. Nothing is persisted — scaling is display-only.

## `mep plan` output

```
Cook plan: Shawarma
  hands-on ~1h33m  ·  total ~2h28m

  Gather:
    - 2 cups yogurt
    - 1/2 cup extra-virgin olive oil
    ...

   1  [active 10m]  Make the yogurt marinade
   2  [passive 2h]  Marinate chicken, cover, refrigerate
       └ Make pita dough and prep tabbouleh while it marinates
   ...
```

## `mep cook` interaction

Loads the cached plan (generates if missing), prints **mise en place** (the
gather list + a deduped equipment list), waits for Enter to begin, then walks
tasks one at a time:

```
Step 2 of 16   [passive ~2h]
  Marinate chicken thighs in the mixture, cover and refrigerate
  uses: chicken thighs, marinade
  equipment: refrigerator
  Coming up (step 4): oven — start preheating now.
  while it waits: Make pita dough and prep tabbouleh

  elapsed 0:42   ⏲ chicken marinade 1:58:20        [Enter] start timer & continue
```

- A live clock redraws the status line every second and shows every running
  background timer.
- On a passive step, **Enter** starts that step's named timer and advances; on
  an active step, **Enter** just advances.
- `preheat_cue` looks ahead up to two steps; if a heat appliance (oven/grill/
  broil/griddle) is coming and the current step isn't already using it, it nudges
  you to preheat now.
- When all steps are done, any unfinished timers are drained (the loop waits and
  rings them as they fire). **Ctrl-C** stops cleanly and reports any timers still
  running with their remaining time.
- Stdlib only: `select.select([sys.stdin], [], [], 1.0)` waits up to a second for
  a keypress while re-ticking. If stdin is not a TTY (piped), each step falls
  back to a plain blocking read.

## Modules

- `mise/plan.py` — Claude call, parse, validate/normalize (`generate_plan`,
  `_normalize_tasks`).
- `mise/cook.py` — interactive loop + pure helpers (`fmt_duration`, `fmt_clock`,
  `status_line`, `timers_summary`, `estimate_wallclock_minutes`, `all_equipment`,
  `preheat_cue`) that are unit-tested.
- `mise/scale.py` — `parse_base_servings`, `scale_quantity` (pure, unit-tested).
- `mise/llm.py` — `create_message` retry wrapper shared with extraction.
- `mise/db.py` — `plan_steps` schema + migration, `save_plan`, `get_plan`.
- `mise/cli.py` — `plan`/`cook`/`show` commands, `_render_plan`, `_gather_lines`.

## Testing (offline)

- `save_plan` / `get_plan` round-trip including the enrichment columns; cascade
  delete with the recipe.
- `_normalize_tasks` coercion and the all-empty error.
- `scale_quantity` (integers, fractions, mixed numbers, ranges, embedded
  numbers, vague pass-through) and `parse_base_servings`.
- `estimate_wallclock_minutes` (passive overlap vs all-active sum),
  `all_equipment` dedupe, `preheat_cue` look-ahead.
- `fmt_duration`, `fmt_clock`, `status_line`.
- The terminal I/O loop stays thin and is not unit-tested.

## Scope guard (YAGNI)

No pause/resume, no multi-recipe coordination, no saved cook sessions, no
config knobs. Scaling is display-only and never written back. Marked
experimental in help text and README.
