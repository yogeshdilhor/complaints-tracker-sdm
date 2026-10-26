"""Take a compiled complaint file (original complaint, SDM letters, replies, all in one PDF)
and work out which pages are which document, so each becomes its own stage on the timeline.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from complaints.services import llm

logger = logging.getLogger(__name__)

PAGE_CHARS = 1800  # the head of a page says what it is
MAX_TOKENS = 3000
MAX_TOKENS_SPLIT_ONLY = 6000  # a 60-page file can hold 30 documents

KINDS = {
    "complaint": "the original complaint, application or the email/printout of it",
    "forwarding": "a higher office (Deputy Commissioner, DRO, ADC) forwarding the complaint on",
    "notice": "a letter by the SDM office marking the complaint to an official for a report",
    "reminder": "a letter by the SDM office reminding an official to send the report",
    "report": "a report or reply from an official or department to the SDM",
    "atr": "an action taken report or portal submission",
    "order": "an order, court order or hearing notice",
    "other": "anything else (enclosures, ID copies, photographs, receipts)",
}
PROMPT = (
    "Below are the pages of one file kept for a complaint at an SDM (sub-divisional "
    "magistrate) office in Haryana, India. The file stitches several separate documents "
    "together, often NOT in date order; a document can run over several pages, and an "
    "enclosure belongs with the letter it is attached to. Split the file into its separate "
    'documents. Reply with one JSON object {{"documents": [...]}} listing every document, '
    "with every page in exactly one document. Each item has:\n"
    '"pages": [first page, last page] (1-based, inclusive)\n'
    '"kind": one of: {kinds}\n'
    '"date": the date written on the document, as YYYY-MM-DD, or "" if none\n'
    '"from": the office or person who wrote it (the letterhead or signature, not the addressee)\n'
    '"to": who it is addressed to\n'
    '"headline": a title of at most 12 words saying what this document does\n'
    '"summary": {summary_rule}\n\n'
    "Pages:\n{pages}"
)


@dataclass
class Segment:
    first: int
    last: int
    kind: str
    date: date | None
    sender: str
    to: str
    headline: str
    summary: str


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


SUMMARY_IN_SPLIT = "50 to 75 words in English: what it says, asks or decides; nothing invented"
SUMMARY_LATER = 'leave as ""'
TOTAL_PAGE_CHARS = 90000  # about what a long-context model reads comfortably in one go


def _repair(segments: list[Segment], page_count: int) -> list[Segment]:
    """Make the split cover every page exactly once: clip overlaps, fill gaps, drop nonsense."""
    cleaned: list[Segment] = []
    next_page = 1
    for s in sorted(segments, key=lambda s: (s.first, s.last)):
        first, last = max(s.first, next_page), min(s.last, page_count)
        if first > last:
            continue
        if first > next_page:  # pages nobody claimed
            cleaned.append(Segment(next_page, first - 1, "other", None, "", "", "Other pages", ""))
        s.first, s.last = first, last
        cleaned.append(s)
        next_page = last + 1
    if next_page <= page_count:
        cleaned.append(Segment(next_page, page_count, "other", None, "", "", "Other pages", ""))
    return cleaned


def segment_pages(
    pages: list[str], provider: str = "sarvam", with_summaries: bool = True
) -> list[Segment]:
    """Ask the model which pages form which document. [] if the reply is unusable."""
    per_page = max(350, min(PAGE_CHARS, TOTAL_PAGE_CHARS // max(len(pages), 1)))
    numbered = "\n\n".join(f"=== PAGE {i} ===\n{t[:per_page]}" for i, t in enumerate(pages, 1))
    kinds = "; ".join(f"{k} ({v})" for k, v in KINDS.items())
    raw = llm.complete(
        PROMPT.format(
            kinds=kinds,
            pages=numbered,
            summary_rule=SUMMARY_IN_SPLIT if with_summaries else SUMMARY_LATER,
        ),
        max_tokens=MAX_TOKENS if with_summaries else MAX_TOKENS_SPLIT_ONLY,
        as_json=True,
        provider_name=provider,
    )
    if not raw:
        return []
    try:
        items = json.loads(raw)["documents"]
        segments = [
            Segment(
                int(i["pages"][0]),
                int(i["pages"][-1]),  # [3] means just page 3
                i.get("kind") if i.get("kind") in KINDS else "other",
                _parse_date(str(i.get("date") or "")),
                str(i.get("from") or "").strip(),
                str(i.get("to") or "").strip(),
                str(i.get("headline") or "").strip(),
                str(i.get("summary") or "").strip(),
            )
            for i in items
        ]
    except (ValueError, KeyError, TypeError, IndexError):
        logger.warning("The page split was not usable: %r", raw[:300])
        return []
    return _repair(segments, len(pages))


def split_pdf(content: bytes, first: int, last: int) -> bytes | None:
    """Pages first..last of a PDF as a new PDF (poppler's pdfseparate and pdfunite)."""
    if not (shutil.which("pdfseparate") and shutil.which("pdfunite")):
        return None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.pdf"
            source.write_bytes(content)
            pattern = str(Path(tmp) / "p-%d.pdf")
            subprocess.run(  # noqa: S603 - fixed arguments
                ["pdfseparate", "-f", str(first), "-l", str(last), str(source), pattern],
                check=True,
                capture_output=True,
                timeout=60,
            )
            parts = [str(Path(tmp) / f"p-{n}.pdf") for n in range(first, last + 1)]
            out = Path(tmp) / "out.pdf"
            if len(parts) == 1:
                return Path(parts[0]).read_bytes()
            subprocess.run(  # noqa: S603
                ["pdfunite", *parts, str(out)], check=True, capture_output=True, timeout=60
            )
            return out.read_bytes()
    except (subprocess.SubprocessError, OSError):
        logger.warning("Could not split the PDF", exc_info=True)
        return None


def order_segments(segments: list[Segment], today: date) -> list[Segment]:
    """Oldest first, with a believable date on each.

    Handwritten and stamped dates are often misread (a 2028 letter, a reminder dated before the
    complaint). A date after today is dropped. Files are usually stitched newest-first, so when
    the readable dates agree with that, the page order is used and a dropped date is set to the
    day after the previous document.
    """
    for s in segments:
        if s.date and s.date > today:
            s.date = None
    dated = sorted((s for s in segments if s.date), key=lambda s: s.first)
    pairs = list(zip(dated, dated[1:], strict=False))
    falling = sum(b.date < a.date for a, b in pairs)  # later pages carry earlier dates
    rising = sum(b.date > a.date for a, b in pairs)
    newest_first_file = falling > rising
    if newest_first_file:
        ordered = sorted(segments, key=lambda s: -s.first)
    else:
        ordered = sorted(segments, key=lambda s: (s.date or today, s.first))
    previous: date | None = None
    for s in ordered:
        if s.date and previous and s.date < previous:
            s.date = None  # out of order with its neighbours, so the reading was wrong
        if s.date is None and previous:
            s.date = min(previous + timedelta(days=1), today)
        if s.date:
            previous = s.date
    return ordered
