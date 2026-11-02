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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from complaints.services import file_splitter, intake, llm
from complaints.services.description_summary import (
    REPORT_PROMPT,
    extract_complaint_fields_unmasked,
)
from complaints.services.documents import (
    Attachment,
    extract,
    page_images,
    sniff_mime,
    store_document,
)
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
    (
        "local",
        "Local reader (free, but poor on handwriting; still uses AI to work out the "
        "documents and fields)",
    ),
    ("none", "Do not use AI at all (fill in everything yourself)"),
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
    """Validate and store the file, then queue the reading. Raises LegacyError.

    A PDF may hold up to LEGACY_MAX_PAGES pages, read and split into separate documents; a
    single JPG/PNG photo is always exactly one page, read as the complaint itself.
    """
    if len(content) > settings.LEGACY_MAX_MB * 1024 * 1024:
        raise LegacyError(f"The file is over {settings.LEGACY_MAX_MB} MB.")
    mime_type = sniff_mime(content)
    if mime_type not in settings.ALLOWED_UPLOAD_TYPES:
        raise LegacyError("Please upload a PDF, JPG or PNG file.")
    if mime_type == "application/pdf":
        pages = count_pages(content)
        if not pages:
            raise LegacyError("This PDF could not be opened.")
        if pages > settings.LEGACY_MAX_PAGES:
            raise LegacyError(
                f"The file has {pages} pages; the limit is {settings.LEGACY_MAX_PAGES}."
            )
    else:
        pages = 1
    if reader not in dict(READERS):
        raise LegacyError("Please choose how the pages are read.")
    if reader != "none" and not llm.is_configured():
        raise LegacyError("OPENAI_API_KEY is not set in the .env file.")
    import hashlib

    sha = hashlib.sha256(content).hexdigest()
    ext = settings.ALLOWED_UPLOAD_TYPES[mime_type]
    path = f"legacy/{sha}{ext}"
    if not default_storage.exists(path):
        default_storage.save(path, ContentFile(content))
    job = LegacyImport.objects.create(
        office=office,
        created_by=user,
        filename=filename[:255],
        sha256=sha,
        file=path,
        mime_type=mime_type,
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


def _slice(content: bytes, mime_type: str, first: int, last: int) -> bytes | None:
    """Pages first..last as their own file. A PDF is cut with poppler; a single photo is
    already exactly one page, so first and last are always 1 and it is returned unchanged."""
    if mime_type == "application/pdf":
        return file_splitter.split_pdf(content, first, last)
    return content


def _read_one_page(
    content: bytes, mime_type: str, number: int, reader: str
) -> tuple[str, bool | None]:
    """The page's text, and whether it looks handwritten (None for the chatgpt reader: reading
    images is exactly what it is good at, so there is nothing to warn about)."""
    part = _slice(content, mime_type, number, number)
    if part is None:
        return "", None
    if reader == "chatgpt":
        images = page_images(part, mime_type, max_pages=1)
        text = llm.read_page(images[0]) if images else None
        return (text or "").strip(), None
    extraction = extract(part, mime_type)
    return extraction.text.strip(), extraction.typed is False


def _read_pages(job: LegacyImport, content: bytes) -> tuple[list[str], bool]:
    """Pages are read several at a time (LEGACY_READ_WORKERS), not one by one: each page is an
    independent round trip to the model, so there is nothing to wait for between them."""
    total = job.pages_total
    pages: list[str] = [""] * total
    failed = 0
    done = 0
    handwritten = False
    with ThreadPoolExecutor(max_workers=settings.LEGACY_READ_WORKERS) as pool:
        futures = {
            pool.submit(_read_one_page, content, job.mime_type, number, job.reader): number
            for number in range(1, total + 1)
        }
        # as_completed() hands results back to this one thread, so updating `pages`, `failed`
        # and the job's progress here needs no lock even though the reading itself is parallel.
        for future in as_completed(futures):
            number = futures[future]
            text, looks_handwritten = future.result()
            pages[number - 1] = text
            handwritten = handwritten or bool(looks_handwritten)
            if not text:
                failed += 1
            done += 1
            _set(job, pages_done=done, step_note=f"Read {done} of {total} pages")
    if failed == total and job.reader == "chatgpt":
        raise LegacyError("No page could be read. Check the OpenAI key and credit balance.")
    return pages, handwritten


def _summarise(text: str, kind: str) -> str:
    prompt = REPORT_PROMPT.format(kind=KIND_LABEL.get(kind, "document"), text=text[:SUMMARY_CHARS])
    return llm.complete(prompt, max_tokens=SUMMARY_TOKENS) or ""


def _with_guessed_fields(fields: dict[str, str], pages: list[str], office) -> dict[str, str]:
    """Fills any gap the model left (or, with no AI at all, everything) with the same free
    pattern-matching the old single-letter intake used, run over the pages' own text."""
    guess = intake.guess_fields("\n\n".join(pages), office)
    fields = dict(fields)
    for key, value in {
        "name": guess.name,
        "phone": guess.phone,
        "address": guess.address,
        "subject": guess.subject,
        "summary": guess.description,
    }.items():
        if not fields.get(key):
            fields[key] = value
    if not fields.get("village") and guess.village_id:
        village = office.village_set.filter(pk=guess.village_id).first()
        if village:
            fields["village"] = village.name
    if not fields.get("village"):
        fields["village_hint"] = guess.village_text
    return fields


def run(job_id: int) -> None:
    """Read, split and summarise. Never raises: a failure is recorded on the import."""
    job = LegacyImport.objects.get(pk=job_id)
    try:
        with default_storage.open(job.file, "rb") as handle:
            content = handle.read()
        _set(job, status=LegacyImport.Status.READING, step_note="Reading the pages")
        pages, handwritten = _read_pages(job, content)
        _set(
            job,
            pages=pages,
            status=LegacyImport.Status.ANALYSING,
            step_note="Working out which pages are which document",
        )
        if job.reader == "none":
            # No AI at all: one bare document (the whole upload), nothing pre-filled, for the
            # user to type in themselves on the next screen.
            segments = [
                file_splitter.Segment(1, job.pages_total, "complaint", None, "", "", "", "")
            ]
            read_dates: dict[int, date | None] = {1: None}
            fields: dict[str, str] = {}
            summaries = [""]
        else:
            segments = file_splitter.segment_pages(pages, with_summaries=False)
            if not segments:  # one more try: a model sometimes answers badly the first time
                segments = file_splitter.segment_pages(pages, with_summaries=False)
            if not segments:
                raise LegacyError(
                    "The AI could not work out the documents in this file. Try again."
                )
            today = timezone.localdate()
            read_dates = {s.first: s.date for s in segments}
            segments = file_splitter.order_segments(segments, today)
            _set(job, status=LegacyImport.Status.SUMMARISING)
            _set(job, step_note=f"Summarising {len(segments)} document(s)")
            original = next((s for s in segments if s.kind == "complaint"), segments[0])
            village_names = list(
                job.office.village_set.filter(is_active=True).values_list("name", flat=True)
            )
            original_text = "\n\n".join(pages[original.first - 1 : original.last])
            texts = ["\n\n".join(pages[s.first - 1 : s.last]) for s in segments]
            # Each document's summary, and the complaint's own fields, are independent of each
            # other, so they go out together rather than one after another.
            with ThreadPoolExecutor(max_workers=settings.LEGACY_READ_WORKERS) as pool:
                fields_future = pool.submit(
                    extract_complaint_fields_unmasked, original_text, village_names
                )
                summaries = list(
                    pool.map(
                        lambda i: (
                            _summarise(texts[i], segments[i].kind) if texts[i].strip() else ""
                        ),
                        range(len(segments)),
                    )
                )
                fields = fields_future.result()
        items = [
            {
                "first": s.first,
                "last": s.last,
                "kind": s.kind,
                "date": s.date.isoformat() if s.date else "",
                "date_unsure": read_dates.get(s.first) != s.date,
                "sender": s.sender,
                "to": s.to,
                "headline": s.headline,
                "summary": summary,
            }
            for s, summary in zip(segments, summaries, strict=True)
        ]
        fields = _with_guessed_fields(fields, pages, job.office)
        _set(
            job,
            status=LegacyImport.Status.READY,
            step_note="",
            proposal={"fields": fields, "documents": items, "handwritten": handwritten},
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
    mime_type: str,
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
    """One document of the old file: its own file, its own summary, its own dated stage."""
    part = _slice(content, mime_type, first, last)
    if part is None:
        raise LegacyError("The file could not be cut into documents.")
    ext = settings.ALLOWED_UPLOAD_TYPES[mime_type]
    document = store_document(
        complaint,
        Attachment(
            part,
            f"{filename_stem}-pages-{first}-{last}{ext}",
            mime_type,
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
        complainant = None
        if data["phone"]:
            complainant = Complainant.objects.filter(office=job.office, phone=data["phone"]).first()
        if complainant is None:
            complainant = Complainant.objects.create(
                office=job.office,
                name=data["name"],
                phone=data["phone"],
                address=data["address"],
                village=data["village"],
            )
        # Flags an AI-assisted reading for a spot check; nothing to check when nothing read it.
        tags = []
        if job.reader != "none":
            tags = [Tag.objects.get_or_create(office=job.office, name=TAG_LEGACY)[0]]
        if data.get("new_tag"):
            existing = Tag.objects.filter(office=job.office, name__iexact=data["new_tag"]).first()
            tags.append(existing or Tag.objects.create(office=job.office, name=data["new_tag"]))
        tags += list(data.get("tags") or [])
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
            tags=tags,
            related_to=data.get("related_to"),
            attachment=Attachment(
                content,
                job.filename,
                job.mime_type,
                extracted_text="\n\n".join(job.pages),
                text_status=Document.TextStatus.DONE,
                page_count=job.pages_total,
                typed=True,
            ),
            at=stage_time(received_on),
        )
        # The one document found is the whole upload (the ordinary case for a fresh, single
        # letter): the "Complaint received" stage above already carries this exact file, so a
        # second, separately-named copy of the same content on its own stage would just
        # duplicate it. A real old file, with more than one document, is unaffected.
        whole_file = (
            len(documents) == 1
            and documents[0]["first"] == 1
            and documents[0]["last"] == job.pages_total
        )
        if whole_file and data["description"]:  # the summary still belongs on the document
            Document.objects.filter(complaint=complaint, kind=Document.Kind.COMPLAINT).update(
                summary=data["description"][:2000]
            )
        for item in documents:  # oldest first
            if whole_file:
                continue
            add_history_stage(
                complaint,
                actor,
                content,
                job.mime_type,
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
        if data.get("department"):  # marking it starts the department's reply clock
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
        remarks="Marked when registered",
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
