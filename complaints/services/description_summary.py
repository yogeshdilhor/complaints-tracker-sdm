"""Short 50-75 word summaries of letters and reports from a small OpenAI model.

Best effort: without an API key, or when the call fails, callers keep the full text.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

API_URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT_SECONDS = 30
MAX_OUTPUT_TOKENS = 300  # Hindi needs many tokens per word
MIN_WORDS_TO_SUMMARISE = 75
PROMPT = (
    "Summarise this complaint letter in 50 to 75 words, in the same language as the letter. "
    "Keep the problem, the place and any key facts or demands. Do not add anything that is "
    "not in the letter. Reply with the summary only.\n\n{text}"
)


REPORT_MAX_CHARS = 12000  # a 10-page report; keeps one call cheap
REPORT_PROMPT = (
    "This is a {kind} document in a complaint file. Summarise what it says in 50 to 75 words, "
    "in the same language as the document: what was done or found, by whom, and the outcome "
    "or recommendation. Do not add anything that is not in the document. "
    "Reply with the summary only.\n\n{text}"
)


def _complete(prompt: str, *, max_tokens: int = MAX_OUTPUT_TOKENS, as_json: bool = False):
    """One chat completion; None when there is no key or the call fails."""
    if not settings.OPENAI_API_KEY:
        return None
    payload = {
        "model": settings.OPENAI_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if as_json:
        payload["response_format"] = {"type": "json_object"}
    body = json.dumps(payload).encode()
    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        API_URL,
        data=body,
        headers={
            "authorization": f"Bearer {settings.OPENAI_API_KEY}",
            "content-type": "application/json",
        },
    )
    logger.info("Summarising %d characters with %s", len(prompt), settings.OPENAI_MODEL)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            data = json.load(response)
        summary = data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as error:
        logger.warning("Summary failed: HTTP %s %s", error.code, error.read()[:300])
        return None
    except Exception:  # network, JSON or shape errors: never block the page
        logger.warning("Summary failed; using the full text", exc_info=True)
        return None
    logger.info("Summary done: %d words", len(summary.split()))
    return summary or None


def summarise_description(text: str) -> str:
    """A 50-75 word summary of `text`; the original text if it is already short or on failure."""
    if len(text.split()) <= MIN_WORDS_TO_SUMMARISE:
        return text
    return _complete(PROMPT.format(text=text)) or text


def summarise_report(text: str, kind: str) -> str | None:
    """A 50-75 word summary of a document's text; None if it could not be made."""
    return _complete(REPORT_PROMPT.format(kind=kind, text=text[:REPORT_MAX_CHARS]))


FIELDS_MAX_CHARS = 6000
FIELDS_MAX_TOKENS = 700
FIELDS_PROMPT = (
    "You read a complaint letter received by a sub-divisional magistrate's office in Haryana, "
    "India. The text comes from OCR and may contain mistakes. Reply with one JSON object with "
    "these keys, using an empty string when the letter does not say:\n"
    '"summary": 50 to 75 words, in the same language as the letter: the problem, the place, '
    "key facts and what the person asks for; nothing that is not in the letter\n"
    '"subject": the complaint title in at most 12 words, same language as the letter\n'
    '"name": the complainant\'s own name\n'
    '"phone": the complainant\'s 10-digit mobile number, digits only\n'
    '"address": the complainant\'s address as written\n'
    '"village": the village the complaint is about, chosen from this list if it matches, '
    "otherwise as written: {villages}\n"
    '"tag": a short 2-4 word label for the kind of complaint, in English, such as '
    '"village road encroachment" or "pension not paid"\n\nLetter:\n{text}'
)


def extract_complaint_fields(text: str, village_names: list[str]) -> dict[str, str]:
    """Summary, subject, name, phone, address, village and tag read from a letter by the model.

    Returns {} when there is no key or the call fails; the caller then keeps its own guesses.
    """
    raw = _complete(
        FIELDS_PROMPT.format(villages=", ".join(village_names[:300]), text=text[:FIELDS_MAX_CHARS]),
        max_tokens=FIELDS_MAX_TOKENS,
        as_json=True,
    )
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("Field extraction returned something that is not JSON")
        return {}
    keys = ("summary", "subject", "name", "phone", "address", "village", "tag")
    return {k: str(data.get(k) or "").strip() for k in keys} if isinstance(data, dict) else {}
