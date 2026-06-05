"""Turn a transcript into a structured recipe with Claude.

The model returns a single JSON object. We parse it defensively: strip code
fences, slice from the first '{' to the last '}', then json.loads. Missing keys
are tolerated downstream via .get, so a partial response still stores cleanly.
"""

import json

from .errors import MepError
from .llm import complete, complete_vision

SYSTEM_PROMPT = """You extract the structured recipe(s) from cooking content: a \
video transcript, a recipe web page, a pasted note, or a photo of a recipe (a \
cookbook page, a recipe card, or a video frame).

The content is often messy: an auto-generated transcript (no punctuation, \
run-on, with filler talk and sponsor reads), a web page with navigation and a \
long life-story preamble wrapped around the actual recipe, or a photo where the \
text is at an angle or handwritten. When several images are given, they are \
pages or frames of the SAME recipe unless they clearly show different dishes. \
Recover the recipe(s) as best you can.

Return ONLY a single JSON object, no markdown fences and no commentary, with \
exactly this shape:
{
  "recipes": [
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
    }
  ]
}

How many recipes to return:
- Most videos teach ONE dish: return a single-element list.
- Return multiple ONLY when the video clearly teaches several independent, \
separately-served dishes (e.g. "3 weeknight dinners", "I made 4 dips").
- Do NOT split one dish into several. Sub-preparations that are part of one \
finished dish (a sauce, dough, marinade, spice mix, or side served as part of \
it) stay as the steps and ingredients of that ONE recipe, not separate recipes.
- If the video is NOT a cooking recipe, return {"recipes": []}. Do not invent a \
recipe.

For each recipe:
- Preserve vague quantities exactly as spoken ("a handful", "to taste", "a \
splash"). Never normalize or convert units.
- Use null for anything the transcript does not state. Never guess servings, \
cook_time, or difficulty.
- Each step is one concise instruction, in order. When a step uses a specific \
pot, pan, or vessel, name it ("In a large skillet, sear...", "Bring a large pot \
of salted water to a boil"), preferring what the source says and otherwise the \
sensible default for the action. Don't invent oddly specific gear.
- tags: 2-6 short lowercase labels (cuisine, course, main technique or \
ingredient) when inferable; otherwise an empty array."""


def extract_recipes(transcript: str, *, title: str | None, config: dict) -> list[dict]:
    """Extract one or more recipes from a transcript. Returns a list of parsed
    recipe dicts (usually one); an empty list when the video is not a recipe."""
    user_content = f"Source title: {title or '(unknown)'}\n\n{transcript}"
    text = complete(config, system=SYSTEM_PROMPT, user=user_content, max_tokens=4096)
    return _parse_recipes(_parse_json(text))


def extract_recipes_from_images(images: list, *, title: str | None, config: dict) -> list[dict]:
    """Extract one or more recipes from photo(s) of a recipe. `images` is a list
    of (media_type, raw_bytes). Returns parsed recipe dicts (empty if none)."""
    user_content = (
        f"Source: {title or 'a photo of a recipe'}\n\n"
        "Read the recipe(s) in the attached image(s) and extract them."
    )
    text = complete_vision(
        config, system=SYSTEM_PROMPT, user=user_content, images=images, max_tokens=4096
    )
    return _parse_recipes(_parse_json(text))


def _parse_recipes(data: dict) -> list[dict]:
    """Pull the recipe list out of the parsed JSON, keeping only entries that
    name a dish (a dish_name of null is the model's signal for 'not a recipe')."""
    recipes = data.get("recipes")
    if not isinstance(recipes, list):
        return []
    return [r for r in recipes if isinstance(r, dict) and r.get("dish_name")]


def _parse_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise MepError(f"Claude did not return JSON. Got: {text[:200]}")
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise MepError(f"Could not parse Claude JSON: {exc}")
