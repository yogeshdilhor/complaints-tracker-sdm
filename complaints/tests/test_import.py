"""Loading a folder of scanned complaints, and keeping ID numbers out of the stored text."""

import io
from datetime import date

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from complaints.management.commands import import_complaints as imp
from complaints.models import Complaint, Document, Platform, State, Tag
from complaints.services.documents import Extraction, redact_ids
from complaints.services.timeline import build_timeline

LETTER = (
    "विषय :- सड़क की मरम्मत बाबत।\nप्रार्थीगण\nरामलाल पुत्र मोहन सिंह निवासी गांव तोहाना\n"
    "मो. 98765 43210\nदिनांक 09.07.2026\n"
)


def pdf(root, relative, body=b"x"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 " + body)
    return path


@pytest.fixture
def folder(tmp_path):
    pdf(tmp_path, "SS/BDPO/SRS-1111111111.pdf", b"one")
    pdf(tmp_path, "SS/BDPO/Reopened/SRS-2222222222 bdpo reopen.pdf", b"two")
    pdf(tmp_path, "SS/DHBVN/SRS-3333333333.pdf", b"three")
    # the very same file in two offices' folders, and once more as reopened
    pdf(tmp_path, "SS/BDPO/SRS-4444444444.pdf", b"same")
    pdf(tmp_path, "SS/BDPO/Reopened/SRS-4444444444.pdf", b"same")
    pdf(tmp_path, "SS/DCRIM/SRS-5555555555.pdf", b"shared")
    pdf(tmp_path, "SS/DHBVN/SRS-5555555555.pdf", b"shared")
    pdf(tmp_path, "NAIB TEHSILDAR GORIWALA/415 Govt 19-12-2022 Hari Ram.pdf", b"nt")
    return tmp_path


@pytest.fixture
def reader(monkeypatch):
    calls = []

    def fake(content, mime):
        calls.append(content)
        return Extraction(LETTER, "done", 3, typed=True)

    monkeypatch.setattr(imp, "extract", fake)
    return calls


def run(folder, *args, actor):
    out = io.StringIO()
    call_command("import_complaints", str(folder), "--actor", actor.username, *args, stdout=out)
    return out.getvalue()


# --------------------------------------------------------------------- parsing


def test_samadhan_shivir_files_take_their_office_and_reference_from_the_path(tmp_path):
    today = date(2026, 10, 11)
    path = pdf(tmp_path, "SS/BDPO/Reopened/SRS-1166917916 bdpo odhan reopen.pdf")
    s = imp.parse_source(tmp_path, path, today)
    assert (s.platform, s.office_folder, s.reopened) == (Platform.SAMADHAN_SHIVIR, "BDPO", True)
    assert s.external_ref == "SRS-1166917916"
    plain = imp.parse_source(tmp_path, pdf(tmp_path, "SS/DHBVN/SRS-358852599.pdf"), today)
    assert plain.office_folder == "DHBVN" and not plain.reopened


def test_other_folders_are_internal_and_the_file_name_carries_the_date(tmp_path):
    path = pdf(tmp_path, "NAIB TEHSILDAR GORIWALA/415 Govt 19-12-2022 Hari Ram.pdf")
    s = imp.parse_source(tmp_path, path, date(2026, 10, 11))
    assert s.platform == Platform.INTERNAL and s.office_folder == "NAIB TEHSILDAR GORIWALA"
    assert s.name_date == date(2022, 12, 19) and s.external_ref.startswith("415 Govt")


def test_dates_are_read_sensibly():
    today = date(2026, 10, 11)
    assert imp.date_from_filename("x 26-03-2025 y.pdf", today) == date(2025, 3, 26)
    assert imp.date_from_filename("x 45-13-2025.pdf", today) is None
    assert imp.date_from_filename("no date.pdf", today) is None
    assert imp.date_from_text("स्टाम्प 9/7/26 \n दिनांक 09.07.2026", today) == date(2026, 7, 9)
    assert imp.date_from_text("दिनांक ०९.०७.२०२६", today) == date(2026, 7, 9)  # Devanagari digits
    assert imp.date_from_text("dated 01.01.2099", today) is None  # not in the future
    assert imp.date_from_text("nothing here", today) is None
    # the labelled date wins over an earlier one that is not
    assert imp.date_from_text("12/05/2024 ... दिनांक 03.04.2026", today) == date(2026, 4, 3)


def test_known_offices_get_their_proper_names():
    assert imp.office_names("DHBVN") == ("Executive Engineer, DHBVN", "EXIN DHBVN")
    assert imp.office_names("Some New Office") == ("Some New Office", "Some New Office")


# ------------------------------------------------------------------ the import


def test_dry_run_plans_but_changes_nothing(folder, reader, clerk, office):
    out = run(folder, "--dry-run", actor=clerk)
    assert "8 PDF files = 6 different complaints" in out
    assert "Dry run" in out and "REOPENED" in out and "same file in 2 places" in out
    assert Complaint.objects.count() == 0 and reader == []


def test_files_become_complaints_marked_to_their_office(folder, reader, clerk, office):
    run(folder, actor=clerk)
    assert Complaint.objects.count() == 6  # 8 files; two files appear twice
    ss = Complaint.objects.get(external_ref="SRS-1111111111")
    assert ss.source_platform == Platform.SAMADHAN_SHIVIR
    assert ss.current_state == State.AWAITING_REPORT
    assert ss.current_department.short_name == "BDPO"
    assert ss.received_on == date(2026, 7, 9)  # the letter's own date
    assert ss.complainant.name == "रामलाल" and ss.complainant.phone == "9876543210"
    assert str(ss.village) == "तोहाना"
    assert ss.subject == "सड़क की मरम्मत बाबत।"
    assert ss.created_by == clerk and ss.documents.get().kind == Document.Kind.COMPLAINT
    assert ss.documents.get().extracted_text == LETTER
    # the marking is dated when it was received, so the clock is honest
    assert ss.referrals.get().issued_on == date(2026, 7, 9)


def test_internal_letters_use_the_file_name_date(folder, reader, clerk, office):
    run(folder, actor=clerk)
    nt = Complaint.objects.get(external_ref__startswith="415 Govt")
    assert nt.source_platform == Platform.INTERNAL
    assert nt.received_on == date(2022, 12, 19)
    assert nt.current_department.name == "Naib Tehsildar, Goriwala"


def test_the_reopened_folder_tags_and_marks_the_complaint(folder, reader, clerk, office):
    run(folder, actor=clerk)
    for ref in ("SRS-2222222222", "SRS-4444444444"):
        c = Complaint.objects.get(external_ref=ref)
        assert Tag.objects.get(name="Reopened") in c.tags.all(), ref
        assert "Re-opened (from the Reopened folder)" in [s.title for s in build_timeline(c)]
    plain = Complaint.objects.get(external_ref="SRS-1111111111")
    assert Tag.objects.get(name="Reopened") not in plain.tags.all()


def test_the_same_file_in_two_folders_is_one_complaint(folder, reader, clerk, office):
    run(folder, actor=clerk)
    assert Complaint.objects.filter(external_ref="SRS-4444444444").count() == 1
    shared = Complaint.objects.get(external_ref="SRS-5555555555")
    assert shared.current_department.short_name == "DCRIM"
    titles = [s.title for s in build_timeline(shared)]
    assert "Also found in the DHBVN folder" in titles


def test_everything_loaded_is_flagged_for_checking(folder, reader, clerk, office):
    run(folder, actor=clerk)
    verify = Tag.objects.get(name="Imported - verify")
    assert verify.complaints.count() == Complaint.objects.count()


def test_running_it_again_loads_nothing_twice(folder, reader, clerk, office):
    run(folder, actor=clerk)
    reads = len(reader)
    again = run(folder, actor=clerk)
    assert Complaint.objects.count() == 6 and "already loaded" in again
    assert len(reader) == reads  # and the files were not read a second time


def test_a_missing_folder_or_login_is_refused(tmp_path, clerk, office):
    with pytest.raises(CommandError):
        run(tmp_path / "nope", actor=clerk)
    clerk.username = "ghost"
    with pytest.raises(CommandError):
        run(tmp_path, actor=clerk)


# ----------------------------------------------------------------- ID numbers


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Aadhaar 4821 6093 7715 end", "Aadhaar [ID number removed] end"),
        ("no spaces 482160937715 x", "no spaces [ID number removed] x"),
        ("dashes 4821-6093-7715", "dashes [ID number removed]"),
        ("फोन ४८२१ ६०९३ ७७१५", "फोन [ID number removed]"),
        ("phone 9876501234 stays", "phone 9876501234 stays"),
        ("mobile 98765 01234 stays", "mobile 98765 01234 stays"),
        ("portal ref SRS-1808582106", "portal ref SRS-1808582106"),
    ],
)
def test_aadhaar_numbers_are_removed_but_phones_and_references_are_not(text, expected):
    assert redact_ids(text) == expected


def test_every_extraction_is_redacted(monkeypatch):
    from complaints.services import documents

    monkeypatch.setattr(
        documents,
        "_extract",
        lambda c, m: Extraction("card 4821 6093 7715", "done", 1, latin_text="4821 6093 7715"),
    )
    result = documents.extract(b"x", "application/pdf")
    assert "4821" not in result.text and "4821" not in result.latin_text


@pytest.mark.parametrize(
    "stem, name",
    [
        ("415 Govt 19-12-2022 Hari Ram", "Hari Ram"),
        ("Suresh Kumar 1941 EA 22-12-2023", "Suresh Kumar"),
        ("Anil Kumar Sharma 5076 SK 09-10-2025", "Anil Kumar Sharma"),
        ("Rajesh Singh Advocate 2698 Steno 19-08-2026", "Rajesh Singh Advocate"),
        ("101MISC cea bhagat 26-03-2025 3 Complaints", "bhagat"),
        ("292 pa cea 06-09-2024 dayal patwari", "dayal patwari"),
        ("Mahesh Advocate", "Mahesh Advocate"),
        ("1502CEA 07-07-2025", ""),
    ],
)
def test_a_name_can_be_read_from_the_file_name(stem, name):
    assert imp.name_from_stem(stem) == name


def test_a_letter_without_a_readable_name_uses_the_file_name(folder, monkeypatch, clerk, office):
    monkeypatch.setattr(
        imp, "extract", lambda content, mime: Extraction("विषय :- कुछ\n", "done", 1, typed=True)
    )
    run(folder, actor=clerk)
    assert Complaint.objects.get(external_ref__startswith="415 Govt").complainant.name == (
        "Hari Ram"
    )
    # Samadhan Shivir files have no name in them, so those stay "Unknown" until read properly
    assert Complaint.objects.get(external_ref="SRS-1111111111").complainant.name == "Unknown"


# ------------------------------------------------------- reading them with the AI later


def test_the_ai_pass_fills_gaps_without_overwriting(
    folder, reader, clerk, office, monkeypatch, settings
):
    from complaints.management.commands import reread_complaints as rr

    run(folder, actor=clerk)
    settings.OPENAI_API_KEY, settings.LLM_PROVIDER = "k", "openai"
    c = Complaint.objects.get(external_ref="SRS-3333333333")
    kept_village = str(c.village)
    other = Complaint.objects.get(external_ref="SRS-1111111111")
    other.village = None
    other.save()
    c.complainant.name = "Unknown"
    c.complainant.save()
    original_subject = c.subject
    monkeypatch.setattr(
        rr,
        "extract_complaint_fields",
        lambda text, villages: {
            "summary": "A short summary.",
            "subject": "Better subject",
            "name": "Ram Singh",
            "phone": "9111122222",
            "address": "House 4",
            "village": "Phagu",
            "tag": "housing instalment",
        },
    )
    call_command("reread_complaints", stdout=io.StringIO())
    c.refresh_from_db()
    assert c.description == "A short summary." and c.complainant.name == "Ram Singh"
    assert c.subject == original_subject  # a real subject is kept
    assert str(c.village) == kept_village  # so is a village the reader already found
    other.refresh_from_db()
    assert str(other.village) == "Phagu"  # but a missing one is filled in
    assert {t.name for t in c.tags.all()} >= {"AI-read", "housing instalment"}


def test_the_ai_pass_needs_a_key_and_skips_what_is_done(
    folder, reader, clerk, office, settings, monkeypatch
):
    from complaints.management.commands import reread_complaints as rr

    run(folder, actor=clerk)
    settings.OPENAI_API_KEY = ""
    with pytest.raises(CommandError):
        call_command("reread_complaints", stdout=io.StringIO())
    settings.OPENAI_API_KEY, settings.LLM_PROVIDER = "k", "openai"
    calls = []
    monkeypatch.setattr(
        rr, "extract_complaint_fields", lambda t, v: calls.append(1) or {"summary": "s"}
    )
    call_command("reread_complaints", stdout=io.StringIO())
    first = len(calls)
    call_command("reread_complaints", stdout=io.StringIO())
    assert first == Complaint.objects.count() and len(calls) == first


def test_the_ai_pass_stops_when_the_ai_is_not_answering(
    folder, reader, clerk, office, settings, monkeypatch
):
    from complaints.management.commands import reread_complaints as rr

    run(folder, actor=clerk)
    settings.OPENAI_API_KEY, settings.LLM_PROVIDER = "k", "openai"
    monkeypatch.setattr(rr, "extract_complaint_fields", lambda t, v: {})
    with pytest.raises(CommandError):
        call_command("reread_complaints", stdout=io.StringIO())


def test_the_ai_pass_leaves_out_handwritten_and_poorly_read_letters(
    folder, reader, clerk, office, settings, monkeypatch
):
    from complaints.management.commands import reread_complaints as rr

    run(folder, actor=clerk)
    Document.objects.update(is_typed=False)
    settings.OPENAI_API_KEY, settings.LLM_PROVIDER = "k", "openai"
    calls = []
    monkeypatch.setattr(
        rr, "extract_complaint_fields", lambda t, v: calls.append(1) or {"summary": "s"}
    )
    call_command("reread_complaints", stdout=io.StringIO())
    assert calls == []
    call_command("reread_complaints", "--include-scanned", stdout=io.StringIO())
    assert len(calls) == Complaint.objects.count()
