"""One place that talks to the language-model provider, chosen by the LLM_PROVIDER setting.

Both providers speak the same chat-completions format, so switching is a settings change:
`LLM_PROVIDER=openai` (e.g. gpt-6-luna) or `LLM_PROVIDER=sarvam` (sarvam-105b).
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 60
REASONING_HEADROOM = 1500  # thinking tokens count against the output limit; leave room for them


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    key: str
    model: str
    token_param: str  # name of the output-limit field in the request
    reasoning_effort: str  # "" = leave unset, "off" = turn thinking off, else the level


def provider(name: str | None = None) -> Provider:
    if (name or settings.LLM_PROVIDER) == "anthropic":
        return Provider(
            "anthropic",
            "https://api.anthropic.com/v1/messages",
            settings.ANTHROPIC_API_KEY,
            settings.ANTHROPIC_MODEL,
            "max_tokens",
            "",
        )
    if (name or settings.LLM_PROVIDER) == "sarvam":
        return Provider(
            "sarvam",
            "https://api.sarvam.ai/v1/chat/completions",
            settings.SARVAM_API_KEY,
            settings.SARVAM_MODEL,
            "max_tokens",
            settings.SARVAM_REASONING_EFFORT,
        )
    return Provider(
        "openai",
        "https://api.openai.com/v1/chat/completions",
        settings.OPENAI_API_KEY,
        settings.OPENAI_MODEL,
        "max_completion_tokens",
        settings.OPENAI_REASONING_EFFORT,
    )


def is_configured(name: str | None = None) -> bool:
    return bool(provider(name).key)


def complete_with_images(
    prompt: str, images: list[bytes], *, max_tokens: int, as_json: bool = False
) -> str | None:
    """Ask the OpenAI vision model about page images. Private details in the images are NOT
    masked: that is the price of reading handwriting and seals."""
    chosen = provider("openai")
    if not chosen.key or not images:
        return None
    parts: list[dict] = [{"type": "text", "text": prompt}]
    for image in images:
        mime = "image/png" if image.startswith(b"\x89PNG") else "image/jpeg"
        encoded = base64.b64encode(image).decode()
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    payload: dict = {
        "model": settings.OPENAI_VISION_MODEL,
        "messages": [{"role": "user", "content": parts}],
        chosen.token_param: max_tokens,
    }
    if as_json:
        payload["response_format"] = {"type": "json_object"}
    return _post(chosen, payload, f"{len(images)} page image(s)")


def complete(
    prompt: str, *, max_tokens: int, as_json: bool = False, provider_name: str | None = None
) -> str | None:
    """One chat completion; None when there is no key or the call fails."""
    chosen = provider(provider_name)
    if not chosen.key:
        return None
    payload: dict = {"model": chosen.model, "messages": [{"role": "user", "content": prompt}]}
    if chosen.name == "anthropic":  # a different request shape; JSON is asked for in words
        if as_json:
            payload["messages"][0]["content"] += "\n\nReply with the JSON object only."
        payload["max_tokens"] = max_tokens
        return _post(chosen, payload, f"{len(prompt)} characters")
    effort = chosen.reasoning_effort
    if effort == "off":
        payload["reasoning_effort"] = None
    elif effort:
        payload["reasoning_effort"] = effort
        max_tokens += REASONING_HEADROOM
    payload[chosen.token_param] = max_tokens
    if as_json:
        payload["response_format"] = {"type": "json_object"}
    return _post(chosen, payload, f"{len(prompt)} characters")


def _headers(chosen: Provider) -> dict[str, str]:
    if chosen.name == "anthropic":
        return {
            "x-api-key": chosen.key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    return {
        "authorization": f"Bearer {chosen.key}",
        "api-subscription-key": chosen.key,  # Sarvam's own header; OpenAI ignores it
        "content-type": "application/json",
    }


def _post(chosen: Provider, payload: dict, what: str) -> str | None:
    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        chosen.url,
        data=json.dumps(payload).encode(),
        headers=_headers(chosen),
    )
    logger.info("Asking %s (%s) about %s", chosen.name, payload["model"], what)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            data = json.load(response)
        if chosen.name == "anthropic":
            text = "".join(b.get("text", "") for b in data["content"]).strip()
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        else:
            text = (data["choices"][0]["message"]["content"] or "").strip()
    except urllib.error.HTTPError as error:
        logger.warning("%s call failed: HTTP %s %s", chosen.name, error.code, error.read()[:300])
        return None
    except Exception:  # network, JSON or shape errors: never block the page
        logger.warning("%s call failed", chosen.name, exc_info=True)
        return None
    logger.info("%s answered with %d words", chosen.name, len(text.split()))
    return text or None
