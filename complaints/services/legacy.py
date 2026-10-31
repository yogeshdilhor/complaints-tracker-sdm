"""Legacy complaints: one big scanned file in, a complaint with its whole history out.

The file is read in the background: pages are read (by ChatGPT looking at the page images, or
the free local reader), a model works out which pages form which document, each document is
summarised, and the result waits for the user to check it. Only then is the complaint created,
with one dated timeline stage and one PDF per document.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from datetime import date, datetime, time
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import close_old_connections, transaction
from django.utils import timezone

from complaints.models import (
    Complainant,
    Complaint,
    Document,
    LegacyImport,
    Tag,
)
from complaints.services import file_splitter, llm
from complaints.services.description_summary import (
    REPORT_PROMPT,
    extract_complaint_fields_unmasked,
)
from complaints.services.documents import Attachment, extract, page_images, store_document
from complaints.workflow import Action, add_stage, apply_transition, register_complaint

logger = logging.getLogger(__name__)

SUMMARY_CHARS = 12000
SUMMARY_TOKENS = 400
TAG_LEGACY = "Legacy - verify"

KIND_TO_DOCUMENT = {
    "complaint": Document.Kind.COMPLAINT,
    "forwarding": Document.Kind.OTHER,
    "notice": Document.Kind.NOTICE,
    "reminder": Document.Kind.NOTICE,
    "report": Document.Kind.INQUIRY_REPORT,
    "atr": Document.Kind.ATR,
    "order": Document.Kind.OTHER,
    "other": Document.Kind.OTHER,
}
KIND_LABEL = {
    "complaint": "Complaint",
    "forwarding": "Forwarded",
    "notice": "SDM letter",
    "reminder": "Reminder",
    "report": "Report",
    "atr": "ATR",
    "order": "Order",
    "other": "Document",
}
READERS = [
    ("chatgpt", "ChatGPT gpt-6-luna reads the page images (best for Hindi and handwriting)"),
    ("local", "Local reader (free, but poor on handwriting)"),
]


class LegacyError(Exception):
    """Something the user should be told about in plain words."""


# ------------------------------------------------------------------ upload


def count_pages(content: bytes) -> int:
    try:
        out = subprocess.run(  # noqa: S603, S607 - fixed arguments
            ["pdfinfo", "-"], input=content, capture_output=True, timeout=60, check=True
        ).stdout.decode("utf-8", errors="replace")
    except (subprocess.SubprocessError, OSError):
        return 0
    match = re.search(r"^Pages:\s+(\d+)", out, re.MULTILINE)
    return int(match.group(1)) if match else 0


def create_import(office, user, content: bytes, filename: str, reader: str):
    """Validate and store the file, then queue the reading. Raises LegacyError."""
    if not content.startswith(b"%PDF"):
        raise LegacyError("Please upload a PDF. Scan all pages into one file.")
    if len(content) > settings.LEGACY_MAX_MB * 1024 * 1024:
        raise LegacyError(f"The file is over {settings.LEGACY_MAX_MB} MB.")
    pages = count_pages(content)
    if not pages:
        raise LegacyError("This PDF could not be opened.")
    if pages > settings.LEGACY_MAX_PAGES:
        raise LegacyError(f"The file has {pages} pages; the limit is {settings.LEGACY_MAX_PAGES}.")
    if reader not in dict(READERS):
        raise LegacyError("Please choose how the pages are read.")
    if not llm.is_configured():
        raise LegacyError("OPENAI_API_KEY is not set in the .env file.")
    import hashlib

    sha = hashlib.sha256(content).hexdigest()
    path = f"legacy/{sha}.pdf"
    if not default_storage.exists(path):
        default_storage.save(path, ContentFile(content))
    job = LegacyImport.objects.create(
        office=office,
        created_by=user,
        filename=filename[:255],
        sha256=sha,
        file=path,
        size_bytes=len(content),
        reader=reader,
        analyser="openai",
        pages_total=pages,
    )
    start(job)
    return job


def start(job: LegacyImport) -> None:
    if settings.LEGACY_RUN_INLINE:
        run(job.pk)
    else:
        threading.Thread(target=_in_thread, args=(job.pk,), daemon=True).start()


# ------------------------------------------------------------------ the background work


def _in_thread(job_id: int) -> None:
    close_old_connections()  # a thread needs its own database connection
    try:
        run(job_id)
    finally:
        close_old_connections()


def _set(job: LegacyImport, **fields: Any) -> None:
    LegacyImport.objects.filter(pk=job.pk).update(**fields)
    for name, value in fields.items():
        setattr(job, name, value)


def _read_pages(job: LegacyImport, content: bytes) -> list[str]:
    total = job.pages_total
    pages: list[str] = []
    failed = 0
    for number in range(1, total + 1):
        part = file_splitter.split_pdf(content, number, number)
        text: str | None = None
        if part is not None and job.reader == "chatgpt":
            images = page_images(part, "application/pdf", max_pages=1)
            text = llm.read_page(images[0]) if images else None
        elif part is not None:
            text = extract(part, "application/pdf").text.strip()
        if text is None:
            failed += 1
        pages.append((text or "").strip())
        _set(job, pages_done=len(pages), step_note=f"Read {len(pages)} of {total} pages")
    if failed == total:
        raise LegacyError("No page could be read. Check the OpenAI key and credit balance.")
    return pages


def _summarise(text: str, kind: str) -> str:
    prompt = REPORT_PROMPT.format(kind=KIND_LABEL.get(kind, "document"), text=text[:SUMMARY_CHARS])
    return llm.complete(prompt, max_tokens=SUMMARY_TOKENS) or ""


def run(job_id: int) -> None:
    """Read, split and summarise. Never raises: a failure is recorded on the import."""
    job = LegacyImport.objects.get(pk=job_id)
    try:
        with default_storage.open(job.file, "rb") as handle:
            content = handle.read()
        _set(job, status=LegacyImport.Status.READING, step_note="Reading the pages")
        pages = _read_pages(job, content)
        _set(
            job,
            pages=pages,
            status=LegacyImport.Status.ANALYSING,
            step_note="Working out which pages are which document",
        )
        segments = file_splitter.segment_pages(pages, with_summaries=False)
        if not segments:  # one more try: a model sometimes answers badly the first time
            segments = file_splitter.segment_pages(pages, with_summaries=False)
        if not segments:
            raise LegacyError("The AI could not work out the documents in this file. Try again.")
        today = timezone.localdate()
        read_dates = {s.first: s.date for s in segments}
        segments = file_splitter.order_segments(segments, today)
        _set(job, status=LegacyImport.Status.SUMMARISING)
        items = []
        for number, s in enumerate(segments, 1):
            _set(job, step_note=f"Summarising document {number} of {len(segments)}")
            text = "\n\n".join(pages[s.first - 1 : s.last])
            items.append(
                {
                    "first": s.first,
                    "last": s.last,
                    "kind": s.kind,
                    "date": s.date.isoformat() if s.date else "",
                    "date_unsure": read_dates.get(s.first) != s.date,
                    "sender": s.sender,
                    "to": s.to,
                    "headline": s.headline,
                    "summary": _summarise(text, s.kind) if text.strip() else "",
                }
            )
        original = next((s for s in segments if s.kind == "complaint"), segments[0])
        village_names = list(
            job.office.village_set.filter(is_active=True).values_list("name", flat=True)
        )
        fields = extract_complaint_fields_unmasked(
            "\n\n".join(pages[original.first - 1 : original.last]),
            village_names,
        )
        _set(
            job,
            status=LegacyImport.Status.READY,
            step_note="",
            proposal={"fields": fields, "documents": items},
        )
    except LegacyError as error:
        _set(job, status=LegacyImport.Status.FAILED, error=str(error), step_note="")
    except Exception:  # never leave an import stuck on "reading"
        logger.exception("Legacy import %s failed", job_id)
        _set(
            job,
            status=LegacyImport.Status.FAILED,
            error="Something went wrong while reading the file. Please try again.",
            step_note="",
        )


# ------------------------------------------------------------------ creating the complaint


def stage_time(day: date | None) -> datetime:
    if day is None:
        return timezone.now()
    return timezone.make_aware(datetime.combine(day, time(10, 0)))


def add_history_stage(
    complaint: Complaint,
    actor,
    content: bytes,
    pages: list[str],
    first: int,
    last: int,
    *,
    kind: str,
    day: date | None,
    headline: str,
    sender: str,
    to: str,
    summary: str,
    unsure_date: bool,
    filename_stem: str,
) -> None:
    """One document of the old file: its own PDF, its own summary, its own dated stage."""
    part = file_splitter.split_pdf(content, first, last)
    if part is None:
        raise LegacyError("The PDF could not be cut into documents.")
    document = store_document(
        complaint,
        Attachment(
            part,
            f"{filename_stem}-pages-{first}-{last}.pdf",
            "application/pdf",
            extracted_text="\n\n".join(pages[first - 1 : last]),
            text_status=Document.TextStatus.DONE,
            page_count=last - first + 1,
            typed=True,
        ),
        KIND_TO_DOCUMENT.get(kind, Document.Kind.OTHER),
        actor,
    )
    Document.objects.filter(pk=document.pk).update(summary=summary[:2000], is_typed=True)
    note = f"{sender} to {to}".strip() if sender or to else ""
    if unsure_date:
        note += " (The date on the document could not be read reliably; placed by order.)"
    add_stage(
        complaint,
        actor,
        f"{KIND_LABEL.get(kind, 'Document')}: {headline}"[:200],
        note=note.strip(),
        documents=[document],
        at=stage_time(day),
    )


def create_complaint(job: LegacyImport, actor, data: dict[str, Any], documents: list[dict]):
    """Build the complaint and its history from the checked proposal. `documents` are the
    items the user kept, each with its (possibly edited) headline."""
    with default_storage.open(job.file, "rb") as handle:
        content = handle.read()
    dates = [date.fromisoformat(d["date"]) for d in documents if d["date"]]
    received_on = min([data["received_on"], *dates])
    stem = job.filename.rsplit(".", 1)[0][:60]
    with transaction.atomic():
        complainant = Complainant.objects.create(
            office=job.office,
            name=data["name"],
            phone=data["phone"],
            address=data["address"],
            village=data["village"],
        )
        tag, _ = Tag.objects.get_or_create(office=job.office, name=TAG_LEGACY)
        complaint = register_complaint(
            office=job.office,
            complainant=complainant,
            actor=actor,
            source_platform=data["source_platform"],
            received_on=received_on,
            subject=data["subject"],
            description=data["description"],
            external_ref=data["external_ref"],
            village=data["village"],
            tags=[tag],
            attachment=Attachment(
                content,
                job.filename,
                "application/pdf",
                extracted_text="\n\n".join(job.pages),
                text_status=Document.TextStatus.DONE,
                page_count=job.pages_total,
                typed=True,
            ),
            at=stage_time(received_on),
        )
        for item in documents:  # oldest first
            add_history_stage(
                complaint,
                actor,
                content,
                job.pages,
                item["first"],
                item["last"],
                kind=item["kind"],
                day=date.fromisoformat(item["date"]) if item["date"] else None,
                headline=item["headline"],
                sender=item["sender"],
                to=item["to"],
                summary=item["summary"],
                unsure_date=item["date_unsure"],
                filename_stem=stem,
            )
        _bring_up_to_date(complaint, actor, data, dates)
        job.complaint = complaint
        job.status = LegacyImport.Status.DONE
        job.save(update_fields=["complaint", "status", "updated_at"])
    return complaint


def _bring_up_to_date(complaint: Complaint, actor, data: dict[str, Any], dates: list[date]):
    """Move the complaint to where it stands now, after its history."""
    latest = max(dates) if dates else timezone.localdate()
    base = stage_time(latest)
    apply_transition(
        complaint,
        Action.ISSUE_NOTICE,
        actor,
        department=data["department"],
        remarks="Added from a legacy file",
        at=base,
    )
    state = data["current_state"]
    if state in ("review", "closed"):
        apply_transition(complaint, Action.RECORD_REPORT, actor, at=base)
    if state == "closed":
        apply_transition(complaint, Action.REVIEW_SATISFIED, actor, at=base)
        apply_transition(
            complaint,
            Action.CLOSE_WITHOUT_PORTAL,
            actor,
            reason=data["closed_reason"] or "Closed before it was added to the portal",
            at=base,
        )
