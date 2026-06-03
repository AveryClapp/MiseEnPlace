"""Generate a cooking timeline for a recipe with Claude.

We store steps but no durations, so this makes one call that estimates per-task
time, classifies each as hands-on or hands-off, and reorders tasks to slot prep
into waits. Output is the same defensively parsed JSON style as extraction.
"""

from anthropic import Anthropic

from .config import EXTRACTION_MODEL
from .errors import MiseError
from .extract import _parse_json

SYSTEM_PROMPT = """You are a kitchen timing assistant. Given a recipe's steps, \
produce an efficient cooking timeline.

Reorder the tasks to front-load prep and to slot hands-on work into hands-off \
waits, so the cook is never idle while there is prep to do. Keep every cooking \
action from the original steps; you may merge trivial ones.

Return ONLY a single JSON object, no markdown fences and no commentary, with \
exactly this shape:
{
  "tasks": [
    {"instruction": string, "duration_minutes": number, "mode": "active" | "passive", "overlap_hint": string | null}
  ]
}

Rules:
- "active" = hands-on work. "passive" = a hands-off wait (marinating, baking, \
resting, unattended simmering).
- duration_minutes: your best estimate of how long that task takes, in minutes.
- overlap_hint: for a passive wait, a short suggestion of what to prep during \
it (drawn from later steps). Use null for active tasks or when nothing fits.
- Keep instructions concise and in the order they should be done."""


def generate_plan(recipe_data: dict, *, api_key: str, model: str = EXTRACTION_MODEL) -> list[dict]:
    """Return an ordered list of task dicts. `recipe_data` is db.get_recipe()."""
    user_content = _format_input(recipe_data)
    client = Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as exc:  # noqa: BLE001
        raise MiseError(f"Plan generation failed: {exc}")

    text = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
    data = _parse_json(text)
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise MiseError("Claude returned no tasks for the plan.")
    return tasks


def _format_input(recipe_data: dict) -> str:
    recipe = recipe_data["recipe"]
    name = recipe["dish_name"] or recipe["title"] or "(unknown dish)"
    ingredients = ", ".join(
        ing["name"] for ing in recipe_data["ingredients"] if ing.get("name")
    )
    steps = "\n".join(
        f"{s['step_number']}. {s['instruction']}" for s in recipe_data["steps"]
    )
    return f"Dish: {name}\n\nIngredients: {ingredients}\n\nSteps:\n{steps}"
