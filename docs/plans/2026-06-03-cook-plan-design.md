# Cook plan + cook-along — Design

**Date:** 2026-06-03
**Status:** Implemented (experimental)

## Purpose

Make actually cooking a stored recipe seamless. Two commands:

- `mep plan <id>` — AI-generated, cached cooking timeline for one recipe.
- `mep cook <id>` — a manual-advance walkthrough of that timeline with a live
  pacing timer per step.

Experimental: timings are AI estimates, not ground truth.

## Generation

We store steps but no durations, so `plan` makes one Claude call. Input: the
recipe's dish name, ingredient names, and ordered steps. Output JSON:

```json
{"tasks": [
  {"instruction": str, "duration_minutes": number,
   "mode": "active" | "passive", "overlap_hint": str | null}
]}
```

- `mode`: `active` = hands-on; `passive` = hands-off wait (marinate, bake, rest).
- Claude **reorders** tasks to front-load prep and slot hands-on work into
  hands-off waits, so you are never idle when there is prep to do.
- `overlap_hint`: for a passive wait, a short "do this now" drawn from later
  steps; null otherwise.

The result is cached so `cook` and re-display reuse it. ~1 to 2 cents per plan,
once per recipe. Same defensive JSON parsing as extraction.

## Storage

```sql
CREATE TABLE plan_steps (
  id, recipe_id REFERENCES recipes(id) ON DELETE CASCADE,
  position, instruction, duration_minutes, mode, overlap_hint
);
```

`save_plan` replaces any existing rows for the recipe (regenerate is a clean
overwrite). `get_plan` returns the ordered tasks, or `[]` if none. The table is
created by `init_db`, which the `plan`/`cook` commands call first so existing
databases pick it up.

## Execution model

The plan is a **linear, ordered** task list: do them top to bottom. Reordering
is what creates the efficiency; we do not model parallel background timers.
`overlap_hint` is advisory text. Summary line reports hands-on time (sum of
active durations) and total time (sum of all durations).

## `mep plan` output

```
Cook plan: Shawarma   (hands-on ~45m  ·  total ~3h15m)

   1  [active 15m]  Make the yogurt marinade
   2  [active 5m]   Coat chicken, cover, refrigerate
       └ make pita dough and prep tabbouleh while it marinates
   3  [passive 2h]  Chicken marinates
   ...
```

## `mep cook` interaction

Loads the cached plan (generates if missing), then walks tasks one at a time:

```
Step 2 of 9   [active ~5m]
  Coat chicken in marinade, cover, refrigerate.
  └ next: make pita dough

  elapsed 0:42        [Enter] next  ·  [Ctrl-C] quit
```

- A live clock redraws the status line every second. `active` counts up
  (elapsed); `passive` counts down from the estimate and rings the terminal
  bell at zero. Neither auto-advances.
- **Enter** moves to the next step. **Ctrl-C** quits cleanly.
- Stdlib only: `select.select([sys.stdin], [], [], 1.0)` waits up to a second
  for a keypress while re-ticking. If stdin is not a TTY (piped), fall back to a
  plain blocking `input()` per step.

## Modules

- `mise/plan.py` — Claude call + parse (`generate_plan`).
- `mise/cook.py` — interactive loop + pure helpers (`fmt_duration`,
  `fmt_clock`, `status_line`) that are unit-tested.
- `mise/db.py` — `plan_steps` schema, `save_plan`, `get_plan`.
- `mise/cli.py` — `plan` and `cook` commands, `render_plan`.

## Testing (offline)

- `save_plan` / `get_plan` round-trip; cascade delete with the recipe.
- `fmt_duration`, `fmt_clock`, `status_line` (passive remaining, passive over,
  active elapsed).
- The terminal I/O loop stays thin and is not unit-tested.

## Scope guard (YAGNI)

No pause/resume, no multi-recipe coordination, no saved cook sessions, no
config. Marked experimental in help text and README.
