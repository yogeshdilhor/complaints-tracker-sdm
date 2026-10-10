"""One place that talks to the language-model provider, chosen by the LLM_PROVIDER setting.

Both providers speak the same chat-completions format, so switching is a settings change:
`LLM_PROVIDER=openai` (e.g. gpt-6-luna) or `LLM_PROVIDER=sarvam` (sarvam-105b).
"""

from __future__ import annotations

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


def provider() -> Provider:
    if settings.LLM_PROVIDER == "sarvam":
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


def is_configured() -> bool:
    return bool(provider().key)


def complete(prompt: str, *, max_tokens: int, as_json: bool = False) -> str | None:
    """One chat completion; None when there is no key or the call fails."""
    chosen = provider()
    if not chosen.key:
        return None
    payload: dict = {"model": chosen.model, "messages": [{"role": "user", "content": prompt}]}
    effort = chosen.reasoning_effort
    if effort == "off":
        payload["reasoning_effort"] = None
    elif effort:
        payload["reasoning_effort"] = effort
        max_tokens += REASONING_HEADROOM
    payload[chosen.token_param] = max_tokens
    if as_json:
        payload["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        chosen.url,
        data=json.dumps(payload).encode(),
        headers={"authorization": f"Bearer {chosen.key}", "content-type": "application/json"},
    )
    logger.info("Asking %s (%s) about %d characters", chosen.name, chosen.model, len(prompt))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            data = json.load(response)
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except urllib.error.HTTPError as error:
        logger.warning("%s call failed: HTTP %s %s", chosen.name, error.code, error.read()[:300])
        return None
    except Exception:  # network, JSON or shape errors: never block the page
        logger.warning("%s call failed", chosen.name, exc_info=True)
        return None
    logger.info("%s answered with %d words", chosen.name, len(text.split()))
    return text or None
