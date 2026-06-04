"""Estimate a recipe's nutrition with one model call.

A rough macro estimate from the ingredient list: calories, protein, carbs, and
fat for the whole recipe, plus the model's guess at how many servings it makes.
Approximate by nature, so it is labelled "estimated" everywhere it surfaces.
"""

from .errors import MepError
from .extract import _parse_json
from .llm import complete

SYSTEM_PROMPT = """You estimate the nutrition of a recipe from its ingredients.

Return ONLY a single JSON object, no markdown fences and no commentary, with \
exactly this shape:
{
  "calories": number,
  "protein_g": number,
  "carbs_g": number,
  "fat_g": number,
  "servings": number,
  "note": string | null
}

The four macro values are totals for the ENTIRE recipe (all ingredients \
combined). servings is your best estimate of how many servings the full recipe \
makes. note is an optional one-line caveat. Estimate from typical food values; \
this is an approximation, not a lab measurement."""


def estimate_macros(recipe_data: dict, *, config: dict) -> dict:
    """Return {'macros': {...}, 'servings': float|None, 'note': str|None}.
    `recipe_data` is db.get_recipe()."""
    if not recipe_data["ingredients"]:
        raise MepError("This recipe has no ingredients to analyze.")
    text = complete(
        config, system=SYSTEM_PROMPT, user=_format_input(recipe_data), max_tokens=600
    )
    return _normalize(_parse_json(text))


def _normalize(data: dict) -> dict:
    def num(key):
        try:
            return max(0.0, float(data.get(key)))
        except (TypeError, ValueError):
            return None

    macros = {
        "calories": num("calories"),
        "protein_g": num("protein_g"),
        "carbs_g": num("carbs_g"),
        "fat_g": num("fat_g"),
    }
    if all(v is None for v in macros.values()):
        raise MepError("The model did not return usable macros.")
    note = data.get("note")
    return {
        "macros": macros,
        "servings": num("servings"),
        "note": str(note).strip() if note else None,
    }


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
    return f"Dish: {name}\n\nIngredients:\n{ingredients}"
