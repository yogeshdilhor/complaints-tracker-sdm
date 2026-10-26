"""Hide private details from text before it is sent to a language model.

The portal keeps everything; only the copy sent out is masked. Patterns catch ID, phone, email
and bank numbers. The complainant's own name, phone and address are known to the caller, so
those exact values (and each word of the name) are replaced too.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from complaints.services.documents import redact_ids

_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[PAN]"),
    (re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b"), "[IFSC]"),
    # Mobile numbers, with or without +91 / 0 and with spaces or dashes inside.
    (
        re.compile(r"(?<![\d०-९])(?:\+?91[ \-]?|0)?[6-9०-९](?:[ \-]?[\d०-९]){9}(?![\d०-९])"),
        "[phone]",
    ),
    # Bank account numbers and other long digit runs.
    (re.compile(r"(?<![\d०-९])[\d०-९]{9,18}(?![\d०-९])"), "[number]"),
]
_GENERIC = {"sarpanch", "multiple", "complainants", "gram", "panchayat"}  # roles, not identities
_MIN_WORD = 3  # shorter name words ("Ram", "Lal" are fine; "of", "S" are not worth masking)


def mask_private(text: str, known: Iterable[str] = ()) -> str:
    """`text` with ID numbers, phones, emails, bank numbers and the `known` strings masked."""
    text = redact_ids(text)
    for pattern, label in _PATTERNS:
        text = pattern.sub(label, text)
    pieces: set[str] = set()
    for value in known:
        value = (value or "").strip()
        if len(value) >= _MIN_WORD and value.casefold() not in _GENERIC:
            pieces.add(value)
            pieces.update(w for w in re.split(r"[\s,]+", value) if len(w) >= _MIN_WORD)
    pieces = {p for p in pieces if p.casefold() not in _GENERIC}
    for piece in sorted(pieces, key=len, reverse=True):  # longest first, so "Ram Lal" beats "Ram"
        text = re.sub(re.escape(piece), "[private]", text, flags=re.IGNORECASE)
    return text
