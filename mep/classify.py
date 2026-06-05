"""Classify a recipe into a meal type and a 1-10 health score (one small call).

Used to power `mep discover`: it lets you ask for a random breakfast, a healthy
dinner, or something to pig out on. Both values are the model's judgment from the
dish and its ingredients, so they are approximate. Computed at ingest and
backfilled by `mep classify`.
"""

from .extract import _parse_json
from .llm import complete

MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack", "sweets")
_ALLOWED = set(MEAL_TYPES)

SYSTEM_PROMPT = """You classify a cooking recipe into a meal type and a health \
score.

Return ONLY a single JSON object, no markdown fences and no commentary, with \
exactly this shape:
{
  "meal_type": "breakfast" | "lunch" | "dinner" | "snack" | "sweets",
  "health_score": integer from 1 to 10
}

- meal_type: the single best fit for when this dish is usually eaten. Use \
"sweets" for desserts and sweet treats (cakes, cookies, candy, ice cream).
- health_score: 1 means very indulgent (rich, fried, sugary, heavy), 10 means \
very healthy (lean, vegetable-forward, minimally processed). Judge from the \
ingredients and how it is cooked, and use the full range."""


def classify_recipe(recipe_data: dict, *, config: dict) -> dict:
    """Return {'meal_type': str|None, 'health_score': int|None}.
    `recipe_data` is db.get_recipe()."""
    text = complete(
        config, system=SYSTEM_PROMPT, user=_format_input(recipe_data), max_tokens=100
    )
    return _normalize(_parse_json(text))


def _normalize(data: dict) -> dict:
    meal_type = data.get("meal_type")
    meal_type = meal_type.strip().lower() if isinstance(meal_type, str) else None
    if meal_type not in _ALLOWED:
        meal_type = None
    try:
        health = int(round(float(data.get("health_score"))))
        health = max(1, min(10, health))
    except (TypeError, ValueError):
        health = None
    return {"meal_type": meal_type, "health_score": health}


def _format_input(recipe_data: dict) -> str:
    recipe = recipe_data["recipe"]
    name = recipe["dish_name"] or recipe["title"] or "(unknown dish)"
    ingredients = "\n".join(
        "- " + (ing.get("name") or "")
        for ing in recipe_data["ingredients"]
        if ing.get("name")
    )
    return f"Dish: {name}\n\nIngredients:\n{ingredients}"
