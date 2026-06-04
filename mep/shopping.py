"""Combine several recipes' ingredients into one shopping list (one model call).

Merges the same item across recipes, sums quantities where the units are
compatible, groups by grocery aisle, and leaves vague amounts ("to taste")
alone. Display-only: the combined, normalized amounts are never written back to
the database, so stored quantities stay verbatim.
"""

from .errors import MepError
from .extract import _parse_json
from .llm import complete

SYSTEM_PROMPT = """You combine the ingredient lists of several recipes into one \
grocery shopping list.

Return ONLY a single JSON object, no markdown fences and no commentary, with \
exactly this shape:
{
  "sections": [
    {"aisle": "Produce", "items": ["2 lemons", "6 cloves garlic"]}
  ]
}

Group items by common grocery aisle (Produce, Meat & Seafood, Dairy, Pantry, \
Spices, Frozen, Bakery, Other). Merge the same ingredient across recipes and sum \
quantities when the units are compatible (e.g. "4 cloves" + "2 cloves" -> "6 \
cloves garlic"). If amounts are vague or incompatible ("a handful", "to taste"), \
keep them as-is, combined onto one line. Each item is a single human-readable \
string. Omit empty aisles."""


def build_list(recipes: list[dict], *, config: dict) -> list[dict]:
    """`recipes` is a list of db.get_recipe() dicts. Returns ordered sections:
    [{"aisle": str, "items": [str, ...]}, ...]."""
    blocks = [_format_recipe(r) for r in recipes if r["ingredients"]]
    if not blocks:
        raise MepError("None of those recipes have ingredients to combine.")
    text = complete(
        config, system=SYSTEM_PROMPT, user="\n\n".join(blocks), max_tokens=1200
    )
    return _normalize(_parse_json(text))


def _normalize(data: dict) -> list[dict]:
    sections = []
    for sec in data.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        items = [str(i).strip() for i in (sec.get("items") or []) if str(i).strip()]
        aisle = (sec.get("aisle") or "Other").strip() or "Other"
        if items:
            sections.append({"aisle": aisle, "items": items})
    if not sections:
        raise MepError("The model did not return a usable shopping list.")
    return sections


def _format_recipe(recipe_data: dict) -> str:
    recipe = recipe_data["recipe"]
    name = recipe["dish_name"] or recipe["title"] or "(unknown dish)"
    ingredients = "\n".join(
        "- " + " ".join(
            p for p in (ing.get("quantity"), ing.get("unit"), ing.get("name")) if p
        )
        for ing in recipe_data["ingredients"]
        if ing.get("name")
    )
    return f"Recipe: {name}\nIngredients:\n{ingredients}"
