"""Rewrite a recipe around what the cook already has.

Light surgery, not reinvention: drop the parts needed to make things the cook
already owns, swap a few ingredients, and otherwise leave the recipe alone. The
result comes back in the same JSON shape as extraction, so it flows straight
back through db.insert_recipe and every existing show/plan/cook path.

`parse_selection` and `parse_subs` are pure (they turn the interactive line
input into a have-list and a sub-map) and are unit-tested; the Claude call is
not.
"""

from .config import EXTRACTION_MODEL
from .errors import MepError
from .extract import _parse_json
from .llm import create_message

SYSTEM_PROMPT = """You adapt a recipe to what the cook already has. Make the \
smallest change that works — the recipe should shift and lose parts, not be \
reinvented.

You are told (a) parts the cook ALREADY HAS made or bought and (b) ingredient \
substitutions. Apply them:

- For a part the cook already has: remove the ingredients and steps that exist \
only to MAKE that part. Keep steps that USE it, lightly reworded to refer to \
the ready-made item (e.g. "make the pita" steps go; "stuff into pita" stays).
- If an ingredient is shared with parts you are keeping, remove only the share \
used by the dropped part; remove it entirely only if nothing else uses it.
- For a substitution: replace the ingredient and adjust wording/amounts only as \
much as the swap requires.
- Change nothing else. Keep vague quantities verbatim ("a handful", "to taste"). \
Do not renumber or pad; just give the steps that remain, in order.

Return ONLY a single JSON object, no markdown fences and no commentary, with \
exactly this shape:
{
  "dish_name": string | null,
  "cook_time": string | null,
  "servings": string | null,
  "difficulty": "easy" | "medium" | "hard" | null,
  "ingredients": [
    {"name": string, "quantity": string | null, "unit": string | null, "prep": string | null}
  ],
  "steps": [string, ...],
  "tags": [string, ...]
}"""


def adapt_recipe(
    recipe_data: dict,
    *,
    have: list[str],
    subs: dict[str, str],
    api_key: str,
    model: str = EXTRACTION_MODEL,
) -> dict:
    """Return an adapted recipe dict (extraction-shaped). `recipe_data` is
    db.get_recipe(); `have` are component/part names already on hand; `subs`
    maps an ingredient to its replacement."""
    if not have and not subs:
        raise MepError("Nothing to adapt — give --have or --sub.")
    message = create_message(
        api_key,
        model=model,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _format_input(recipe_data, have, subs)}],
    )
    text = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
    adapted = _parse_json(text)
    if not isinstance(adapted.get("ingredients"), list) or not isinstance(
        adapted.get("steps"), list
    ):
        raise MepError("Claude did not return an adapted recipe.")
    return adapted


def parse_selection(text: str, count: int) -> list[int]:
    """Turn '1,4' into zero-based indices [0, 3], keeping only 1..count. Junk
    tokens are ignored; an empty line yields []."""
    indices = []
    for token in text.replace(" ", "").split(","):
        if not token:
            continue
        try:
            n = int(token)
        except ValueError:
            continue
        if 1 <= n <= count and (n - 1) not in indices:
            indices.append(n - 1)
    return indices


def parse_subs(text: str) -> dict[str, str]:
    """Turn 'yogurt=sour cream, butter=oil' into {'yogurt': 'sour cream',
    'butter': 'oil'}. Tokens without '=' or an empty side are skipped."""
    subs: dict[str, str] = {}
    for pair in text.split(","):
        if "=" not in pair:
            continue
        left, right = pair.split("=", 1)
        left, right = left.strip(), right.strip()
        if left and right:
            subs[left] = right
    return subs


def _format_input(recipe_data: dict, have: list[str], subs: dict[str, str]) -> str:
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
    lines = [f"Dish: {name}", "", f"Ingredients:\n{ingredients}", "", f"Steps:\n{steps}", ""]
    if have:
        lines.append("Already have (remove what's needed to make these): " + ", ".join(have))
    if subs:
        lines.append(
            "Substitute: " + ", ".join(f"{k} -> {v}" for k, v in subs.items())
        )
    return "\n".join(lines)
