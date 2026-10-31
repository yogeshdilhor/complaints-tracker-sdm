"""The one place that calls a language model. Every call goes to OpenAI's gpt-6-luna."""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 60
PAGE_READ_TOKENS = 3000
REASONING_HEADROOM = 1500  # thinking tokens count against the output limit; leave room for them

MODEL = "gpt-6-luna"  # the only model this app calls; not configurable on purpose
URL = "https://api.openai.com/v1/chat/completions"


def is_configured() -> bool:
    return bool(settings.OPENAI_API_KEY)


def complete_with_images(
    prompt: str, images: list[bytes], *, max_tokens: int, as_json: bool = False
) -> str | None:
    """Ask the model about page images. Private details in the images are NOT masked: that is
    the price of reading handwriting and seals."""
    if not is_configured() or not images:
        return None
    parts: list[dict] = [{"type": "text", "text": prompt}]
    for image in images:
        mime = "image/png" if image.startswith(b"\x89PNG") else "image/jpeg"
        encoded = base64.b64encode(image).decode()
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    payload: dict = {
        "model": MODEL,
        "messages": [{"role": "user", "content": parts}],
        "max_completion_tokens": max_tokens,
    }
    if as_json:
        payload["response_format"] = {"type": "json_object"}
    return _post(payload, f"{len(images)} page image(s)")


TRANSCRIBE_PROMPT = (
    "Transcribe every word on this scanned page exactly as written, in the original language "
    "and script (Hindi or English, typed or handwritten), including stamps and signature "
    "blocks. Reply with the page text only, no commentary. If the page is blank, reply with "
    "nothing."
)


def read_page(image: bytes) -> str | None:
    """The text on one scanned page, read by the model. None when the call fails."""
    return complete_with_images(TRANSCRIBE_PROMPT, [image], max_tokens=PAGE_READ_TOKENS)


def complete(prompt: str, *, max_tokens: int, as_json: bool = False) -> str | None:
    """One chat completion; None when there is no key or the call fails."""
    if not is_configured():
        return None
    payload: dict = {"model": MODEL, "messages": [{"role": "user", "content": prompt}]}
    effort = settings.OPENAI_REASONING_EFFORT
    if effort == "off":
        payload["reasoning_effort"] = None
    elif effort:
        payload["reasoning_effort"] = effort
        max_tokens += REASONING_HEADROOM
    payload["max_completion_tokens"] = max_tokens
    if as_json:
        payload["response_format"] = {"type": "json_object"}
    return _post(payload, f"{len(prompt)} characters")


def _post(payload: dict, what: str) -> str | None:
    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        URL,
        data=json.dumps(payload).encode(),
        headers={
            "authorization": f"Bearer {settings.OPENAI_API_KEY}",
            "content-type": "application/json",
        },
    )
    logger.info("Asking %s about %s", payload["model"], what)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            data = json.load(response)
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except urllib.error.HTTPError as error:
        logger.warning("LLM call failed: HTTP %s %s", error.code, error.read()[:300])
        return None
    except Exception:  # network, JSON or shape errors: never block the page
        logger.warning("LLM call failed", exc_info=True)
        return None
    logger.info("LLM answered with %d words", len(text.split()))
    return text or None
