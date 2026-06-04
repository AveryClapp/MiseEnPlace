"""Turn a transcript into a structured recipe with Claude.

The model returns a single JSON object. We parse it defensively: strip code
fences, slice from the first '{' to the last '}', then json.loads. Missing keys
are tolerated downstream via .get, so a partial response still stores cleanly.
"""

import json

from .errors import MepError
from .llm import complete

SYSTEM_PROMPT = """You extract the structured recipe(s) from a YouTube cooking \
video transcript.

Transcripts are usually auto-generated: no punctuation, no capitalization, \
run-on text, plus filler talk and sponsor reads. Recover the recipe(s) as best \
you can.

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
- Each step is one concise instruction, in order.
- tags: 2-6 short lowercase labels (cuisine, course, main technique or \
ingredient) when inferable; otherwise an empty array."""


def extract_recipes(transcript: str, *, title: str | None, config: dict) -> list[dict]:
    """Extract one or more recipes from a transcript. Returns a list of parsed
    recipe dicts (usually one); an empty list when the video is not a recipe."""
    user_content = f"Video title: {title or '(unknown)'}\n\nTranscript:\n{transcript}"
    text = complete(config, system=SYSTEM_PROMPT, user=user_content, max_tokens=4096)
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
