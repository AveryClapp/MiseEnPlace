"""Generate a cooking timeline for a recipe with Claude.

We store steps but no durations, so this makes one call that estimates per-task
time, classifies each as hands-on or hands-off, names timers for waits, and
lists the ingredients and equipment each step needs. Output is validated and
normalized before it is trusted.
"""

from .errors import MepError
from .extract import _parse_json
from .llm import complete

SYSTEM_PROMPT = """You are a kitchen timing assistant. Given a recipe's steps, \
produce an efficient cooking timeline a cook can follow in real time.

Reorder the tasks to front-load prep and to slot hands-on work into hands-off \
waits, so the cook is never idle while there is prep to do. Keep every cooking \
action from the original steps; you may merge trivial ones. Put oven preheats \
and other "start this early" actions where they belong so equipment is ready \
when needed.

Return ONLY a single JSON object, no markdown fences and no commentary, with \
exactly this shape:
{
  "tasks": [
    {
      "instruction": string,
      "duration_minutes": number,
      "mode": "active" | "passive",
      "overlap_hint": string | null,
      "ingredients": [string, ...],
      "equipment": [string, ...],
      "timer_label": string | null
    }
  ]
}

Rules:
- "active" = hands-on work. "passive" = a hands-off wait (marinating, baking, \
proofing, resting, unattended simmering).
- duration_minutes: best estimate in minutes of how long that task takes.
- overlap_hint: for a passive wait, a short suggestion of what to prep during \
it (drawn from later steps). Use null for active tasks or when nothing fits.
- ingredients: the specific ingredients this step uses, with amounts when the \
recipe gives them (e.g. "2 cups yogurt"). Empty array if none.
- equipment: tools/appliances this step needs (e.g. "oven", "blender", \
"skillet", "spit"). Empty array if none.
- timer_label: for passive tasks, a 1-3 word name for a kitchen timer (e.g. \
"chicken marinade", "dough rise"). Use null for active tasks.
- Keep instructions concise and in the order they should be done."""


def generate_plan(recipe_data: dict, *, config: dict) -> list[dict]:
    """Return a validated, ordered list of task dicts. `recipe_data` is
    db.get_recipe()."""
    text = complete(
        config, system=SYSTEM_PROMPT, user=_format_input(recipe_data), max_tokens=3000
    )
    data = _parse_json(text)
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        raise MepError("The model did not return a task list for the plan.")
    return _normalize_tasks(tasks)


def _normalize_tasks(tasks: list) -> list[dict]:
    """Coerce/clean each task so downstream code can trust the shape."""
    cleaned = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        instruction = str(task.get("instruction") or "").strip()
        if not instruction:
            continue

        mode = task.get("mode")
        mode = mode if mode in ("active", "passive") else "active"

        try:
            duration = max(0.0, float(task.get("duration_minutes")))
        except (TypeError, ValueError):
            duration = 0.0

        cleaned.append(
            {
                "instruction": instruction,
                "duration_minutes": duration,
                "mode": mode,
                "overlap_hint": _clean_str(task.get("overlap_hint")),
                "ingredients": _clean_list(task.get("ingredients")),
                "equipment": _clean_list(task.get("equipment")),
                "timer_label": _clean_str(task.get("timer_label")),
            }
        )

    if not cleaned:
        raise MepError("Claude returned no usable tasks for the plan.")
    return cleaned


def _clean_str(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_list(value):
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _format_input(recipe_data: dict) -> str:
    recipe = recipe_data["recipe"]
    name = recipe["dish_name"] or recipe["title"] or "(unknown dish)"
    ingredients = "\n".join(
        "- " + " ".join(
            p for p in (ing.get("quantity"), ing.get("unit"), ing.get("name")) if p
        )
        for ing in recipe_data["ingredients"]
        if ing.get("name")
    )
    steps = "\n".join(
        f"{s['step_number']}. {s['instruction']}" for s in recipe_data["steps"]
    )
    return f"Dish: {name}\n\nIngredients:\n{ingredients}\n\nSteps:\n{steps}"
