"""Group a recipe into its components with one Claude call.

Recipes are stored flat (an ingredient list and a step list). To let a cook say
"I already have the pita", we need to know which ingredients and which steps
exist only to make the pita. This pass recovers that structure: each component
is a named part of the dish with the ingredients it consumes and the steps that
produce it. Output is validated/normalized before it is trusted.
"""

from .config import EXTRACTION_MODEL
from .errors import MepError
from .extract import _parse_json
from .llm import create_message

SYSTEM_PROMPT = """You break a recipe into its components — the distinct parts a \
cook makes and then combines (e.g. a marinade, a flatbread, a sauce, a salad).

Return ONLY a single JSON object, no markdown fences and no commentary, with \
exactly this shape:
{
  "components": [
    {
      "name": string,
      "purpose": string,
      "ingredients": [string, ...],
      "make_steps": [number, ...]
    }
  ]
}

Rules:
- name: a short label for the part ("Pita bread", "Marinade", "Tabbouleh").
- purpose: one short line on what it is or what it's for.
- ingredients: the ingredient lines this part consumes, with amounts when the \
recipe gives them. An ingredient used by two parts appears in both.
- make_steps: the step NUMBERS that PRODUCE this part. Do not include steps that \
merely use the finished part later (assembly, plating). Use the numbering from \
the steps given.
- Cover the real sub-parts of the dish; don't invent parts that aren't there. A \
simple dish may be a single component. Keep names distinct."""


def analyze_components(recipe_data: dict, *, api_key: str, model: str = EXTRACTION_MODEL) -> list[dict]:
    """Return a validated, ordered list of component dicts. `recipe_data` is
    db.get_recipe()."""
    if not recipe_data["steps"]:
        raise MepError("This recipe has no steps to break down.")
    message = create_message(
        api_key,
        model=model,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _format_input(recipe_data)}],
    )
    text = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
    data = _parse_json(text)
    components = data.get("components")
    if not isinstance(components, list):
        raise MepError("Claude did not return a component list.")
    return _normalize_components(components)


def _normalize_components(components: list) -> list[dict]:
    """Coerce/clean each component so downstream code can trust the shape."""
    cleaned = []
    for comp in components:
        if not isinstance(comp, dict):
            continue
        name = str(comp.get("name") or "").strip()
        if not name:
            continue
        cleaned.append(
            {
                "name": name,
                "purpose": str(comp.get("purpose") or "").strip(),
                "ingredients": _clean_list(comp.get("ingredients")),
                "make_steps": _clean_ints(comp.get("make_steps")),
            }
        )
    if not cleaned:
        raise MepError("Claude returned no usable components.")
    return cleaned


def _clean_list(value):
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _clean_ints(value):
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


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
