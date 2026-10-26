"""Turn one complaint's compiled file into a timeline: one stage per document.

Sarvam reads the pages, the model works out which pages are the original complaint, the SDM's
letters, replies and so on, each document is saved as its own PDF, and each becomes a stage
dated by the date written on it. The compiled file stays where it is. Pages go to Sarvam
unmasked. Use --dry-run to see the split without saving anything.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from complaints.models import Complaint
from complaints.services import file_splitter, legacy, sarvam_docs
from core.models import User


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("ref_no", help="For example C-2025-0003")
        parser.add_argument("--actor", default="sdm")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        complaint = Complaint.objects.filter(ref_no=opts["ref_no"]).first()
        if complaint is None:
            raise CommandError("No such complaint.")
        actor = User.objects.filter(username=opts["actor"]).first()
        if actor is None:
            raise CommandError(f"No login named {opts['actor']}.")
        source = complaint.documents.filter(is_deleted=False).order_by("pk").first()
        if source is None or source.mime_type != "application/pdf":
            raise CommandError("The complaint has no PDF to split.")
        with source.file.open("rb") as handle:
            content = handle.read()
        # A file already read by Sarvam keeps its pages in the stored text, split by "---".
        pages = [p.strip() for p in source.extracted_text.split("\n---\n")]
        if source.page_count != len(pages) or not source.extracted_text.strip():
            pages = sarvam_docs.digitise_pages(content, source.mime_type)
        if not pages:
            raise CommandError("Sarvam could not read the file.")
        self.stdout.write(f"{len(pages)} pages read.")
        segments = file_splitter.segment_pages(pages)
        if not segments:
            raise CommandError("Could not work out the documents in the file.")
        read_dates = {s.first: s.date for s in segments}
        segments = file_splitter.order_segments(segments, timezone.localdate())
        for s in segments:
            when = s.date.isoformat() if s.date else "undated"
            self.stdout.write(
                f"  pp {s.first}-{s.last}  {when}  [{s.kind}]  {s.headline}  ({s.sender} -> {s.to})"
            )
        if opts["dry_run"]:
            return
        made = 0
        for s in segments:
            try:
                legacy.add_history_stage(
                    complaint,
                    actor,
                    content,
                    pages,
                    s.first,
                    s.last,
                    kind=s.kind,
                    day=s.date,
                    headline=s.headline,
                    sender=s.sender,
                    to=s.to,
                    summary=s.summary,
                    unsure_date=read_dates[s.first] != s.date,
                    filename_stem=opts["ref_no"],
                )
            except legacy.LegacyError as error:
                raise CommandError(str(error)) from error
            made += 1
        original = next((s for s in segments if s.kind == "complaint"), None)
        if original is not None and original.date:
            Complaint.objects.filter(pk=complaint.pk).update(received_on=original.date)
        self.stdout.write(self.style.SUCCESS(f"{made} documents added to the timeline."))
