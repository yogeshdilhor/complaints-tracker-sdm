"""Short 50-75 word summaries of letters and reports from a small language model (OpenAI or Sarvam).

Best effort: without an API key, or when the call fails, callers keep the full text.
"""

from __future__ import annotations

import json
import logging

from complaints.services import llm

logger = logging.getLogger(__name__)

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
    return llm.complete(prompt, max_tokens=max_tokens, as_json=as_json)


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


STAGE_MAX_CHARS = 12000
STAGE_MAX_TOKENS = 800
STAGE_OPTIONS = {
    "issue_notice": "a notice or letter marking the complaint to an officer for a report",
    "reissue_notice": "a reminder, or a repeat notice asking an officer for the report again",
    "record_report": "a report received from the officer or department",
    "review_satisfied": "the SDM accepting the report",
    "review_not_satisfied": "the SDM finding the report inadequate and asking again",
    "submit_to_portal": "an action taken report (ATR) uploaded on the portal",
    "portal_accepted": "the portal closing or accepting the complaint",
    "portal_rejected": "the portal sending the complaint back or re-opening it",
    "close_without_portal": "the complaint being resolved and filed",
    "reopen": "the complaint being re-opened",
    "add_stage": "anything else",
}
STAGE_PROMPT = (
    "A new document has just been added to a complaint file at an SDM office in Haryana, India. "
    "The text comes from OCR and may contain mistakes.\n"
    'Complaint: "{subject}". Current stage: {state}. Currently with: {department}. '
    "Portal the complaint came from: {portal}.\n"
    "Decide what this document is. Options:\n{options}\n"
    "Reply with one JSON object, using an empty string for anything the document does not say:\n"
    '"action": exactly one option key above\n'
    '"headline": a title for this stage in at most 12 words, same language as the document\n'
    '"officer": the officer or office that wrote the document, as written\n'
    '"date": the date of the document as YYYY-MM-DD\n'
    '"summary": 50 to 75 words, same language as the document: what happened, who did it and '
    "the outcome or recommendation; nothing that is not in the document\n"
    '"ack_no": a portal acknowledgement or reference number\n'
    '"reason": the reason, if the document sends the complaint back or re-opens it\n'
    '"note": one short sentence of anything else worth recording\n\nDocument:\n{text}'
)


def read_document_as_stage(
    text: str, context: dict[str, str], allowed: list[str]
) -> dict[str, str]:
    """What a newly uploaded document says, as a proposed stage; {} if the model is unavailable.

    `allowed` are the action keys that make sense now; the model must pick one of them.
    """
    options = "\n".join(f"- {key}: {STAGE_OPTIONS[key]}" for key in allowed if key in STAGE_OPTIONS)
    raw = _complete(
        STAGE_PROMPT.format(options=options, text=text[:STAGE_MAX_CHARS], **context),
        max_tokens=STAGE_MAX_TOKENS,
        as_json=True,
    )
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    keys = ("action", "headline", "officer", "date", "summary", "ack_no", "reason", "note")
    result = {k: str(data.get(k) or "").strip() for k in keys}
    if result["action"] not in allowed:
        result["action"] = "add_stage"
    return result
