"""Turn a transcript into a structured recipe with Claude.

The model returns a single JSON object. We parse it defensively: strip code
fences, slice from the first '{' to the last '}', then json.loads. Missing keys
are tolerated downstream via .get, so a partial response still stores cleanly.
"""

import json

from .errors import MepError
from .llm import complete

SYSTEM_PROMPT = """You extract one structured recipe from a YouTube cooking \
video transcript.

Transcripts are usually auto-generated: no punctuation, no capitalization, \
run-on text, plus filler talk and sponsor reads. Recover the recipe as best \
you can.

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
}

Rules:
- If the video is NOT a cooking recipe, set dish_name to null, make \
ingredients, steps, and tags empty arrays, and the other fields null. Do not \
invent a recipe.
- If several recipes appear, extract only the primary/main one.
- Preserve vague quantities exactly as spoken ("a handful", "to taste", "a \
splash"). Never normalize or convert units.
- Use null for anything the transcript does not state. Never guess servings, \
cook_time, or difficulty.
- Each step is one concise instruction, in order.
- tags: 2-6 short lowercase labels (cuisine, course, main technique or \
ingredient) when inferable; otherwise an empty array."""


def extract_recipe(transcript: str, *, title: str | None, config: dict) -> dict:
    user_content = f"Video title: {title or '(unknown)'}\n\nTranscript:\n{transcript}"
    text = complete(config, system=SYSTEM_PROMPT, user=user_content, max_tokens=2000)
    return _parse_json(text)


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
