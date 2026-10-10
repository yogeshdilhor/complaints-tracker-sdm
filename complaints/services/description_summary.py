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


def _complete(prompt: str) -> str | None:
    """One chat completion; None when there is no key or the call fails."""
    if not settings.OPENAI_API_KEY:
        return None
    body = json.dumps(
        {
            "model": settings.OPENAI_MODEL,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()
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
