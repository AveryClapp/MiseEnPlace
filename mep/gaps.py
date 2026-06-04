"""Flag likely holes in an already-extracted recipe with one model call.

This points out things a careful cook would notice as missing (a step that uses
an ingredient that was never listed, a clearly-needed temperature or time that
is absent, an obvious step skipped between two others). It does NOT invent or
fill the missing values, and it does not rewrite the recipe. An empty result
means nothing looked off. Computed on demand and cached, like macros.
"""

from .errors import MepError
from .extract import _parse_json
from .llm import complete

SYSTEM_PROMPT = """You review an already-extracted recipe for likely gaps or \
holes, WITHOUT inventing or filling them.

Return ONLY a single JSON object, no markdown fences and no commentary, with \
exactly this shape:
{
  "gaps": [string, ...]
}

Each gap is one short, specific sentence describing something that looks missing \
or incomplete, for example:
- a step uses an ingredient that is not in the ingredient list (or an ingredient \
is listed but never used)
- a cooking step clearly needs a temperature or a time but states none
- an obvious step appears to be skipped between two others
- the recipe ends without actually finishing or assembling the dish

Only flag things a careful cook would genuinely notice. Do NOT guess the missing \
value, do NOT rewrite the recipe, and do NOT pad the list. If the recipe looks \
complete, return {"gaps": []}."""


def find_gaps(recipe_data: dict, *, config: dict) -> list[str]:
    """Return a list of plain-language gap descriptions (possibly empty).
    `recipe_data` is db.get_recipe()."""
    if not recipe_data["steps"]:
        raise MepError("This recipe has no steps to check.")
    text = complete(
        config, system=SYSTEM_PROMPT, user=_format_input(recipe_data), max_tokens=800
    )
    return _normalize(_parse_json(text))


def _normalize(data: dict) -> list[str]:
    gaps = data.get("gaps")
    if not isinstance(gaps, list):
        return []
    return [str(g).strip() for g in gaps if str(g).strip()]


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
