"""Fill in names, villages and summaries of imported complaints with the AI.

Imported complaints start with what the plain reader could find. Once an AI key works, this
reads each complaint's stored text once and adds a 50-75 word summary, the complainant's name,
phone and address, the village and a tag, without overwriting anything already filled in.
Complaints done are tagged "AI-read" so a second run skips them. Letters flagged as handwritten
or poorly read are left out unless you pass --include-scanned.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from complaints.models import Complaint, Document, Tag
from complaints.services import intake, llm
from complaints.services.description_summary import extract_complaint_fields
from core.models import Village

TAG_DONE = "AI-read"
TAG_VERIFY = "Imported - verify"


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--tag", default=TAG_VERIFY, help="Only complaints with this tag")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument(
            "--include-scanned",
            action="store_true",
            help="Also read letters the reader flagged as handwritten or low quality (by default "
            "only typed ones are sent to the AI)",
        )

    def handle(self, *args, **opts):
        if not llm.is_configured():
            raise CommandError(
                "No AI key is set for the chosen provider. Set it in .env and try again."
            )
        done_tag = None
        todo = (
            Complaint.objects.filter(tags__name=opts["tag"])
            .exclude(tags__name=TAG_DONE)
            .select_related("complainant", "office")
            .distinct()
        )
        if opts["limit"]:
            todo = todo[: opts["limit"]]
        updated = failed = 0
        for complaint in todo:
            documents = complaint.documents.filter(
                kind=Document.Kind.COMPLAINT, is_deleted=False
            ).exclude(extracted_text="")
            if not opts["include_scanned"]:
                documents = documents.exclude(is_typed=False)
            text = "\n".join(documents.values_list("extracted_text", flat=True))
            if not text.strip():
                continue
            names = list(
                Village.objects.filter(office=complaint.office).values_list("name", flat=True)
            )
            ai = extract_complaint_fields(text, names)
            if not ai:
                failed += 1
                self.stdout.write(f"  {complaint.ref_no}: the AI gave no answer")
                if failed >= 3 and not updated:
                    raise CommandError("The AI is not answering (no credit?). Stopping.")
                continue
            self._apply(complaint, ai)
            done_tag = (
                done_tag or Tag.objects.get_or_create(office=complaint.office, name=TAG_DONE)[0]
            )
            complaint.tags.add(done_tag)
            updated += 1
            self.stdout.write(f"  {complaint.ref_no}: {complaint.subject[:50]}")
        self.stdout.write(
            self.style.SUCCESS(f"{updated} complaints read, {failed} without an answer.")
        )

    def _apply(self, complaint: Complaint, ai: dict[str, str]) -> None:
        person = complaint.complainant
        if ai.get("summary") and not complaint.description:
            complaint.description = ai["summary"]
        if ai.get("subject") and complaint.subject.startswith("Complaint SRS"):
            complaint.subject = ai["subject"][:300]
        if ai.get("name") and person.name == "Unknown":
            person.name = ai["name"][:200]
        phone = intake.normalise_phone(ai.get("phone", ""))
        if len(phone) == 10 and not person.phone:
            person.phone = phone
        if ai.get("address") and not person.address:
            person.address = ai["address"][:300]
        if ai.get("village") and complaint.village is None:
            wanted = ai["village"].strip()
            village = Village.objects.filter(office=complaint.office, name__iexact=wanted).first()
            complaint.village = village or Village.objects.create(
                office=complaint.office, name=wanted[:200]
            )
            person.village = person.village or complaint.village
        if ai.get("tag"):
            tag = Tag.objects.filter(office=complaint.office, name__iexact=ai["tag"][:50]).first()
            complaint.tags.add(
                tag or Tag.objects.create(office=complaint.office, name=ai["tag"][:50])
            )
        person.save()
        complaint.save()
