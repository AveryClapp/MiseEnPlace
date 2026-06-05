"""Rewrite a recipe's steps so each names the cookware it uses, with one call.

Backs `mep clarify` for existing recipes; new recipes get the same guidance
baked into extraction. Deliberately conservative: same steps, same order, same
meaning, with only the pot/pan/vessel made explicit. If the model changes the
step count, the originals are kept so nothing is silently dropped.
"""

from .errors import MepError
from .extract import _parse_json
from .llm import complete

SYSTEM_PROMPT = """You make a recipe's steps clearer about cookware.

Given a dish's ingredients and its numbered steps, rewrite each step so it names \
the pot, pan, or vessel it uses when an experienced cook would ("In a large \
skillet, sear...", "Bring a large pot of salted water to a boil").

Rules:
- Keep the SAME steps, in the SAME order, with the same meaning. Do NOT add, \
remove, merge, or split steps.
- Only make the cookware/vessel explicit. Don't change ingredients, quantities, \
times, or technique.
- If a step needs no specific vessel (e.g. "Season to taste"), leave it as is.
- Don't invent oddly specific gear; use the sensible default for the action.

Return ONLY a single JSON object, no markdown fences and no commentary:
{"steps": [string, ...]}
The steps array must have exactly the same number of entries as the input."""


def clarify_steps(recipe_data: dict, *, config: dict) -> list[str]:
    """Return the recipe's steps rewritten to name their cookware. Falls back to
    the original steps if the model returns the wrong count."""
    original = [s["instruction"] for s in recipe_data["steps"]]
    if not original:
        return []
    text = complete(
        config, system=SYSTEM_PROMPT, user=_format_input(recipe_data), max_tokens=2000
    )
    data = _parse_json(text)
    new_steps = data.get("steps")
    if not isinstance(new_steps, list):
        raise MepError("The model did not return rewritten steps.")
    cleaned = [str(s).strip() for s in new_steps if str(s).strip()]
    # A mismatched count means the model added/dropped a step despite the rule;
    # keep the originals rather than corrupt the recipe.
    return cleaned if len(cleaned) == len(original) else original


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
