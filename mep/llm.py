"""One place to call the LLM, with retries on transient failures.

Extraction, planning, component analysis, and adaptation all go through
`complete`, which dispatches to Anthropic (default) or OpenAI based on the
configured provider and returns the response text. A transient blip (overload,
rate limit, timeout, dropped connection) retries with backoff; anything else is
surfaced as a MepError. The OpenAI SDK is an optional dependency, imported
lazily.
"""

import time

from .config import model, provider, require_api_key
from .errors import MepError


def _retryable_types() -> tuple:
    """Transient error classes from whichever SDKs are installed."""
    types: list = []
    for module, names in (
        ("anthropic", ("RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError")),
        ("openai", ("RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError")),
    ):
        try:
            mod = __import__(module)
            types.extend(getattr(mod, n) for n in names if hasattr(mod, n))
        except Exception:  # pragma: no cover - depends on what's installed
            continue
    return tuple(types)


_RETRYABLE: tuple | None = None


def is_retryable(exc: Exception) -> bool:
    # Computed lazily so importing this module (and thus the CLI) doesn't pull in
    # the heavy anthropic/openai SDKs until a model call actually happens.
    global _RETRYABLE
    if _RETRYABLE is None:
        _RETRYABLE = _retryable_types()
    return bool(_RETRYABLE) and isinstance(exc, _RETRYABLE)


def complete(config: dict, *, system: str, user: str, max_tokens: int, max_retries: int = 3) -> str:
    """Send one system+user turn to the configured provider, return the text."""
    name = provider(config)
    call = _openai if name == "openai" else _anthropic
    return _with_retries(
        name, lambda: call(config, system, user, max_tokens), max_retries
    )


def complete_vision(
    config: dict, *, system: str, user: str, images: list, max_tokens: int, max_retries: int = 3
) -> str:
    """Like complete, but sends one or more images alongside the text prompt.
    `images` is a list of (media_type, raw_bytes). Requires a vision-capable
    model (the default Anthropic/OpenAI models are)."""
    name = provider(config)
    call = _openai_vision if name == "openai" else _anthropic_vision
    return _with_retries(
        name, lambda: call(config, system, user, images, max_tokens), max_retries
    )


def _with_retries(provider_name: str, fn, max_retries: int) -> str:
    """Run fn(), retrying transient errors with backoff. MepErrors (missing key
    or package) pass straight through; anything else becomes a clean MepError."""
    label = "OpenAI" if provider_name == "openai" else "Claude"
    delay = 1.5
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except MepError:
            raise  # already actionable (e.g. missing package, missing key)
        except Exception as exc:  # noqa: BLE001
            if is_retryable(exc) and attempt < max_retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise MepError(f"{label} request failed: {exc}")


_TRUNCATED_MSG = (
    "The model's response was cut off at the output length limit. The recipe may "
    "be unusually long; try again, or report it if it keeps happening."
)


def _anthropic(config: dict, system: str, user: str, max_tokens: int) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=require_api_key(config))
    message = client.messages.create(
        model=model(config),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if message.stop_reason == "max_tokens":
        raise MepError(_TRUNCATED_MSG)
    return "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )


def _openai_client(config: dict):
    try:
        from openai import OpenAI
    except ImportError:
        raise MepError(
            "OpenAI support needs the openai package. Install with: "
            "pip install 'mise-en-place[openai]'"
        )
    return OpenAI(api_key=require_api_key(config))


def _openai(config: dict, system: str, user: str, max_tokens: int) -> str:
    response = _openai_client(config).chat.completions.create(
        model=model(config),
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise MepError(_TRUNCATED_MSG)
    return choice.message.content or ""


def _anthropic_vision(config: dict, system: str, user: str, images: list, max_tokens: int) -> str:
    import base64

    from anthropic import Anthropic

    blocks = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(data).decode("ascii"),
            },
        }
        for media_type, data in images
    ]
    blocks.append({"type": "text", "text": user})
    client = Anthropic(api_key=require_api_key(config))
    message = client.messages.create(
        model=model(config),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": blocks}],
    )
    if message.stop_reason == "max_tokens":
        raise MepError(_TRUNCATED_MSG)
    return "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )


def _openai_vision(config: dict, system: str, user: str, images: list, max_tokens: int) -> str:
    import base64

    content: list = [{"type": "text", "text": user}]
    for media_type, data in images:
        b64 = base64.standard_b64encode(data).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}}
        )
    response = _openai_client(config).chat.completions.create(
        model=model(config),
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
    )
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise MepError(_TRUNCATED_MSG)
    return choice.message.content or ""
