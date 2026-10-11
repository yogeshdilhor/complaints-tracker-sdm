"""Load a folder of scanned complaints (PDFs) into the tracker.

Folder rules (edit SOURCES below for a different layout):
  SS/<office>/...        Samadhan Shivir complaints marked to <office>; the SRS-number in the file
                         name is the portal reference. A "Reopened" sub-folder tags them Reopened.
  <any other folder>/    Complaints marked to the office named after the folder; the date in the
                         file name (dd-mm-yyyy) is the date received.

The same file in two folders is one complaint. Each file is read once (OCR); Aadhaar-style
numbers are removed from the stored text. Run with --dry-run first. Safe to run again: files
already loaded are skipped.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from complaints.models import Complainant, Complaint, Document, Platform, Tag
from complaints.services import intake
from complaints.services.documents import Attachment, extract
from complaints.workflow import Action, add_stage, apply_transition, register_complaint
from core.models import Department, Office, User, Village

# folder name -> (full name, short name) of the office the complaints are marked to
OFFICES = {
    "BDPO": ("Block Development and Panchayat Officer, Dabwali", "BDPO"),
    "DHBVN": ("Executive Engineer, DHBVN", "EXIN DHBVN"),
    "DCRIM": ("DCRIM", "DCRIM"),
    "NAIB TEHSILDAR GORIWALA": ("Naib Tehsildar, Goriwala", "NT Goriwala"),
}
REOPENED_FOLDER = "reopened"
TAG_REOPENED = "Reopened"
TAG_VERIFY = "Imported - verify"
_SRS = re.compile(r"SRS-\d+", re.I)
_FILE_DATE = re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})-(20\d{2})(?!\d)")
_DEVANAGARI = str.maketrans("०१२३४५६७८९", "0123456789")
_LABELLED_DATE = re.compile(
    r"(?:दिनांक|दिनाक|dated?)\s*[:\-]?\s*(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})", re.I
)
_ANY_DATE = re.compile(r"(?<!\d)(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(20\d{2})(?!\d)")


@dataclass
class Source:
    path: Path
    top: str  # first folder, e.g. "SS"
    office_folder: str  # the office the file sits under
    reopened: bool
    platform: str
    external_ref: str
    name_date: date | None


@dataclass
class Group:
    """All the files that are one and the same complaint (identical content)."""

    sha: str
    sources: list[Source] = field(default_factory=list)

    @property
    def first(self) -> Source:
        return self.sources[0]

    @property
    def reopened(self) -> bool:
        return any(s.reopened for s in self.sources)

    @property
    def offices(self) -> list[str]:
        return list(dict.fromkeys(s.office_folder for s in self.sources))


def _valid(day: int, month: int, year: int, today: date) -> date | None:
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    return found if 2015 <= found.year and found <= today else None


def date_from_filename(name: str, today: date) -> date | None:
    match = _FILE_DATE.search(name)
    return _valid(int(match[1]), int(match[2]), int(match[3]), today) if match else None


def date_from_text(text: str, today: date) -> date | None:
    """The letter's own date: the one after 'दिनांक'/'Date', else the first sensible date."""
    text = text.translate(_DEVANAGARI)
    for pattern in (_LABELLED_DATE, _ANY_DATE):
        for match in pattern.finditer(text):
            found = _valid(int(match[1]), int(match[2]), int(match[3]), today)
            if found:
                return found
    return None


def parse_source(root: Path, path: Path, today: date) -> Source:
    parts = path.relative_to(root).parts
    top, name = parts[0], parts[-1]
    folders = parts[1:-1]
    is_ss = top.upper() == "SS"
    office = folders[0] if is_ss and folders else top
    reopened = any(f.lower() == REOPENED_FOLDER for f in folders)
    if is_ss:
        ref = _SRS.search(name)
        external_ref = ref.group(0).upper() if ref else path.stem[:100]
        return Source(path, top, office, reopened, Platform.SAMADHAN_SHIVIR, external_ref, None)
    return Source(
        path,
        top,
        office,
        reopened,
        Platform.INTERNAL,
        path.stem[:100],
        date_from_filename(name, today),
    )


# File names look like "415 Govt 19-12-2022 Hari Ram": a diary number, a clerk's initials or
# a heading, the date, and the person. These words are not part of a name.
_NOT_NAME = {
    "misc", "cea", "pa", "steno", "ea", "sk", "govt", "complaint", "complaints", "srs",
}  # fmt: skip


def name_from_stem(stem: str) -> str:
    """The person's name out of a file name, or '' when there is none to find."""
    words = []
    for word in re.split(r"[\s_]+", _FILE_DATE.sub(" ", stem)):
        cleaned = re.sub(r"\d+", "", word).strip(" .,-")
        if len(cleaned) >= 2 and cleaned.lower() not in _NOT_NAME and cleaned.isalpha():
            words.append(cleaned)
    return " ".join(words)[:200]


def office_names(folder: str) -> tuple[str, str]:
    return OFFICES.get(folder.upper(), (folder.title(), folder.title()[:50]))


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("root", help="Folder to load, e.g. /data/complaints")
        parser.add_argument("--actor", default="sdm", help="Login the entries are made by")
        parser.add_argument("--ocr-pages", type=int, default=2, help="Pages to read per file")
        parser.add_argument("--dry-run", action="store_true", help="Show the plan, change nothing")
        parser.add_argument("--limit", type=int, default=0, help="Only the first N files")

    # ------------------------------------------------------------------ plan

    def _groups(self, root: Path, limit: int) -> list[Group]:
        today = timezone.localdate()
        files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
        if limit:
            files = files[:limit]
        by_sha: dict[str, Group] = {}
        for path in files:
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            group = by_sha.setdefault(sha, Group(sha))
            group.sources.append(parse_source(root, path, today))
        # Reopened copies last, so the normal copy is the one that registers the complaint.
        for group in by_sha.values():
            group.sources.sort(key=lambda s: s.reopened)
        return sorted(by_sha.values(), key=lambda g: (g.first.top, g.first.path.name))

    def handle(self, *args, **opts):
        root = Path(opts["root"])
        if not root.is_dir():
            raise CommandError(f"{root} is not a folder.")
        groups = self._groups(root, opts["limit"])
        files = sum(len(g.sources) for g in groups)
        self.stdout.write(f"{files} PDF files = {len(groups)} different complaints")
        for g in groups:
            note = []
            if len(g.sources) > 1:
                note.append(f"same file in {len(g.sources)} places: {', '.join(g.offices)}")
            if g.reopened:
                note.append("REOPENED")
            self.stdout.write(
                f"  {g.first.office_folder:<26} {g.first.external_ref[:34]:<34} {'; '.join(note)}"
            )
        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run: nothing was loaded."))
            return

        offices = Office.objects.all()
        if offices.count() != 1:
            raise CommandError("Expected exactly one office in the database.")
        office = offices.get()
        try:
            actor = User.objects.get(username=opts["actor"])
        except User.DoesNotExist:
            raise CommandError(f"No login called '{opts['actor']}'.") from None
        settings.INTAKE_OCR_MAX_PAGES = opts["ocr_pages"]

        started = time.time()
        loaded = skipped = 0
        for n, group in enumerate(groups, start=1):
            if Document.objects.filter(sha256=group.sha, complaint__office=office).exists():
                skipped += 1
                self.stdout.write(f"[{n}/{len(groups)}] already loaded: {group.first.path.name}")
                continue
            complaint = self._load(group, office, actor)
            loaded += 1
            self.stdout.write(
                f"[{n}/{len(groups)}] {complaint.ref_no}  {group.first.external_ref[:30]:<30} "
                f"{group.first.office_folder[:22]:<22} {complaint.received_on}  "
                f"{complaint.complainant.name[:22]:<22} {str(complaint.village or '-')[:14]:<14} "
                f"{complaint.subject[:44]}"
            )
        minutes = (time.time() - started) / 60
        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {loaded} loaded, {skipped} already there ({minutes:.1f} min)."
            )
        )

    # ---------------------------------------------------------------- one complaint

    def _tag(self, office: Office, name: str) -> Tag:
        return Tag.objects.get_or_create(office=office, name=name)[0]

    def _department(self, office: Office, folder: str) -> Department:
        full, short = office_names(folder)
        existing = Department.objects.filter(office=office, name__iexact=full).first()
        return existing or Department.objects.create(
            office=office,
            name=full,
            short_name=short,
            kind=Department.Kind.EXTERNAL_DEPARTMENT,
        )

    def _load(self, group: Group, office: Office, actor: User) -> Complaint:
        source = group.first
        content = source.path.read_bytes()
        extraction = extract(content, "application/pdf")
        today = timezone.localdate()
        received = (
            date_from_text(extraction.text, today) or source.name_date or today
            if source.platform == Platform.SAMADHAN_SHIVIR
            else source.name_date or date_from_text(extraction.text, today) or today
        )
        at = timezone.make_aware(datetime.combine(received, dtime(10, 30)))

        guess = intake.guess_fields(extraction.text, office, extraction.latin_text)
        with transaction.atomic():
            village = None
            if guess.village_id:
                village = Village.objects.get(pk=guess.village_id)
            elif len(guess.village_text) >= 2:
                village = Village.objects.get_or_create(
                    office=office, name=guess.village_text[:200]
                )[0]
            complainant = None
            if guess.phone:
                complainant = Complainant.objects.filter(office=office, phone=guess.phone).first()
            if complainant is None:
                fallback = (
                    name_from_stem(source.path.stem) if source.platform == Platform.INTERNAL else ""
                )
                complainant = Complainant.objects.create(
                    office=office,
                    name=(guess.name or fallback or "Unknown")[:200],
                    phone=guess.phone,
                    address=guess.address,
                    village=village,
                )
            tags = [self._tag(office, TAG_VERIFY)]
            if group.reopened:
                tags.append(self._tag(office, TAG_REOPENED))
            attachment = Attachment(
                content=content,
                filename=source.path.name[:255],
                mime_type="application/pdf",
                extracted_text=extraction.text,
                text_status=extraction.status,
                page_count=extraction.pages,
                typed=extraction.typed,
            )
            complaint = register_complaint(
                office=office,
                complainant=complainant,
                actor=actor,
                source_platform=source.platform,
                received_on=received,
                subject=(guess.subject or f"Complaint {source.external_ref}")[:300],
                description="",
                external_ref=source.external_ref,
                village=village,
                tags=tags,
                attachment=attachment,
                at=at,
            )
            department = self._department(office, source.office_folder)
            apply_transition(
                complaint,
                Action.ISSUE_NOTICE,
                actor,
                department=department,
                remarks=f"Loaded from the folder: {source.office_folder}",
                at=at,
            )
            if group.reopened:
                add_stage(
                    complaint,
                    actor,
                    "Re-opened (from the Reopened folder)",
                    note=f"Found in {source.office_folder} / Reopened.",
                )
            for other in group.offices[1:]:
                add_stage(
                    complaint,
                    actor,
                    f"Also found in the {other} folder",
                    note="The same document is in more than one office's folder. Check which "
                    "office this belongs to.",
                )
        return complaint
