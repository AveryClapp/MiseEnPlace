"""Suggest what to serve with a dish, with one model call.

Returns two things: a few "generic" ideas (a side, a drink, a finishing touch
that need not be in your collection), and matches drawn from recipes you already
have. The matches become undirected edges in `recipe_pairings`, so the whole
collection turns into a "what goes with what" graph over time. Opt-in: only run
at ingest with `mep add --pair`, or on demand with `mep pair`.
"""

from .errors import MepError
from .extract import _parse_json
from .llm import complete

SYSTEM_PROMPT = """You suggest what to serve alongside a main dish.

You are given the dish and a numbered list of OTHER recipes the user already \
has. Return ONLY a single JSON object, no markdown fences and no commentary, \
with exactly this shape:
{
  "generic": [ {"name": string, "why": string} ],
  "matches": [ {"id": integer, "why": string} ]
}

- generic: 2-3 complementary things to serve with the dish (a side, a drink, a \
sauce, a salad, bread...). These are general ideas and need NOT be in the \
user's list. Keep each "why" to a short phrase.
- matches: the ids of recipes FROM THE USER'S LIST that would genuinely pair \
well with this dish, each with a short reason. Use only ids that appear in the \
list. If none fit, return an empty array. Do not force matches."""


def suggest_pairings(recipe_data: dict, candidates: list[dict], *, config: dict) -> dict:
    """Return {'generic': [{name, why}], 'matches': [{id, why}]}. `candidates`
    is db.pairing_candidates(): other recipes the matches may reference."""
    text = complete(
        config, system=SYSTEM_PROMPT,
        user=_format_input(recipe_data, candidates), max_tokens=700,
    )
    return _normalize(_parse_json(text), {c["id"] for c in candidates})


def _normalize(data: dict, valid_ids: set) -> dict:
    generic = []
    for item in data.get("generic") or []:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if name:
            generic.append({"name": name, "why": (item.get("why") or "").strip()})
    matches = []
    seen = set()
    for item in data.get("matches") or []:
        if not isinstance(item, dict):
            continue
        try:
            rid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if rid in valid_ids and rid not in seen:  # ignore hallucinated ids
            seen.add(rid)
            matches.append({"id": rid, "why": (item.get("why") or "").strip()})
    if not generic and not matches:
        raise MepError("The model did not return any pairings.")
    return {"generic": generic, "matches": matches}


def _format_input(recipe_data: dict, candidates: list[dict]) -> str:
    recipe = recipe_data["recipe"]
    name = recipe["dish_name"] or recipe["title"] or "(unknown dish)"
    bits = [f"Dish: {name}"]
    if recipe["meal_type"]:
        bits.append(f"Meal type: {recipe['meal_type']}")
    if recipe_data["tags"]:
        bits.append("Tags: " + ", ".join(recipe_data["tags"]))
    if candidates:
        lines = []
        for c in candidates:
            extra = " ".join(
                p for p in (c.get("meal_type"), " ".join(c.get("tags") or [])) if p
            )
            suffix = f"  [{extra}]" if extra else ""
            lines.append(f"{c['id']}. {c['dish_name']}{suffix}")
        bits.append("Other recipes you have:\n" + "\n".join(lines))
    else:
        bits.append("Other recipes you have: (none yet)")
    return "\n\n".join(bits)
