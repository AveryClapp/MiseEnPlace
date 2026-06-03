"""Turn a transcript into a structured recipe with Claude.

The model returns a single JSON object. We parse it defensively: strip code
fences, slice from the first '{' to the last '}', then json.loads. Missing keys
are tolerated downstream via .get, so a partial response still stores cleanly.
"""

import json

from anthropic import Anthropic

from .config import EXTRACTION_MODEL
from .errors import MiseError

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


def extract_recipe(
    transcript: str,
    *,
    title: str | None,
    api_key: str,
    model: str = EXTRACTION_MODEL,
) -> dict:
    client = Anthropic(api_key=api_key)
    user_content = f"Video title: {title or '(unknown)'}\n\nTranscript:\n{transcript}"
    try:
        message = client.messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as exc:  # noqa: BLE001
        raise MiseError(f"Claude extraction failed: {exc}")

    text = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
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
        raise MiseError(f"Claude did not return JSON. Got: {text[:200]}")
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise MiseError(f"Could not parse Claude JSON: {exc}")
