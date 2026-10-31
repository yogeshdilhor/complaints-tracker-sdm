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
from complaints.services.description_summary import (
    extract_complaint_fields,
    extract_complaint_fields_from_images,
)
from complaints.services.documents import page_images
from core.models import Village

TAG_DONE = "AI-read"
TAG_VISION = "Vision-read"
TAG_VERIFY = "Imported - verify"


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--tag", default=TAG_VERIFY, help="Only complaints with this tag")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument(
            "--vision",
            action="store_true",
            help="Send the page images to the OpenAI vision model instead of the text",
        )
        parser.add_argument(
            "--include-scanned",
            action="store_true",
            help="Also read letters the reader flagged as handwritten or low quality (by default "
            "only typed ones are sent to the AI)",
        )

    def _handle_vision(self, opts):
        """Read the page images of each complaint's letter with the OpenAI vision model.

        Page images are sent unmasked. The complainant's name read from the letter replaces
        whatever is there; other fields only fill gaps.
        """
        if not llm.is_configured():
            raise CommandError("OPENAI_API_KEY is not set.")
        tag = None
        todo = (
            Complaint.objects.filter(tags__name=opts["tag"])
            .exclude(tags__name=TAG_VISION)
            .select_related("complainant", "office")
            .distinct()
            .order_by("pk")
        )
        if opts["limit"]:
            todo = todo[: opts["limit"]]
        updated = failed = 0
        for complaint in todo:
            document = complaint.documents.filter(
                kind=Document.Kind.COMPLAINT, is_deleted=False
            ).first()
            if document is None:
                continue
            with document.file.open("rb") as handle:
                images = page_images(handle.read(), document.mime_type)
            names = list(
                Village.objects.filter(office=complaint.office).values_list("name", flat=True)
            )
            ai = extract_complaint_fields_from_images(images, names) if images else {}
            if not ai:
                failed += 1
                self.stdout.write(f"  {complaint.ref_no}: no answer")
                if failed >= 3 and not updated:
                    raise CommandError("OpenAI is not answering (no credit?). Stopping.")
                continue
            self._apply(complaint, ai, replace_name=True)
            if ai.get("summary"):
                Document.objects.filter(pk=document.pk).update(summary=ai["summary"][:2000])
            tag = tag or Tag.objects.get_or_create(office=complaint.office, name=TAG_VISION)[0]
            complaint.tags.add(tag)
            updated += 1
            self.stdout.write(
                f"  {complaint.ref_no}: {complaint.complainant.name} | {complaint.subject[:50]}"
            )
        self.stdout.write(self.style.SUCCESS(f"{updated} read, {failed} without an answer."))

    def handle(self, *args, **opts):
        if opts["vision"]:
            return self._handle_vision(opts)
        if not llm.is_configured():
            raise CommandError("OPENAI_API_KEY is not set. Set it in .env and try again.")
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
            # Name, phone and address are read here with patterns, not by the AI; whatever is
            # found (or already on file) is masked out of the text the AI sees.
            guess = intake.guess_fields(text, complaint.office)
            person = complaint.complainant
            known = [
                guess.name,
                guess.phone,
                guess.address,
                person.name,
                person.phone,
                person.address,
            ]
            ai = extract_complaint_fields(text, names, known=[k for k in known if k != "Unknown"])
            if ai:
                ai |= {"name": guess.name, "phone": guess.phone, "address": guess.address}
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

    def _apply(self, complaint: Complaint, ai: dict[str, str], replace_name: bool = False) -> None:
        person = complaint.complainant
        if ai.get("summary") and (replace_name or not complaint.description):
            complaint.description = ai["summary"]
        if ai.get("subject") and (replace_name or complaint.subject.startswith("Complaint SRS")):
            complaint.subject = ai["subject"][:300]
        if ai.get("name") and (replace_name or person.name == "Unknown"):
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
