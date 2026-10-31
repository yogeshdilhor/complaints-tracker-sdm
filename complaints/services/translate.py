"""On-demand translation of a complaint's summary: asked for once, then kept on the complaint."""

from __future__ import annotations

import re

from complaints.services import llm

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

TRANSLATE_MAX_CHARS = 4000
TRANSLATE_MAX_TOKENS = 600
TRANSLATE_PROMPT = (
    "Translate the following text to {target}. Keep names, place names, numbers and amounts "
    "exactly as written. Reply with the translation only, nothing else.\n\n{text}"
)


def detect_lang(text: str) -> str:
    """ "hi" if the text has Devanagari script in it, else "en"."""
    return "hi" if _DEVANAGARI.search(text or "") else "en"


def translate(text: str, to: str) -> str | None:
    """`text` in the other language ("en" or "hi"); None if the model is unavailable or fails."""
    target = "English" if to == "en" else "Hindi"
    prompt = TRANSLATE_PROMPT.format(target=target, text=text[:TRANSLATE_MAX_CHARS])
    return llm.complete(prompt, max_tokens=TRANSLATE_MAX_TOKENS)
