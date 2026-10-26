"""A compiled file becomes one timeline stage per document."""

import io
import json
from datetime import date

from django.core.management import call_command

from complaints.models import Document
from complaints.services import file_splitter as fs

TODAY = date(2026, 10, 11)


def seg(first, last, d, kind="notice"):
    return fs.Segment(first, last, kind, d, "SDM", "Official", f"h{first}", "s")


def test_a_newest_first_file_is_ordered_by_page_and_bad_dates_are_repaired():
    segments = [
        seg(4, 4, date(2024, 3, 25)),  # before the complaint: misread
        seg(7, 7, date(2025, 10, 6), "complaint"),
        seg(6, 6, date(2025, 10, 13), "forwarding"),
        seg(5, 5, date(2025, 10, 17)),
        seg(3, 3, date(2028, 6, 4)),  # in the future: misread
        seg(1, 2, date(2026, 8, 19)),
    ]
    ordered = fs.order_segments(segments, TODAY)
    assert [s.first for s in ordered] == [7, 6, 5, 4, 3, 1]
    dates = [s.date for s in ordered]
    assert dates == sorted(dates) and all(d <= TODAY for d in dates)


def test_a_chronological_file_is_ordered_by_date():
    ordered = fs.order_segments([seg(2, 2, date(2025, 5, 2)), seg(1, 1, date(2025, 5, 1))], TODAY)
    assert [s.first for s in ordered] == [1, 2]


def test_the_split_is_repaired_to_cover_every_page_once(monkeypatch):
    reply = json.dumps(
        {
            "documents": [
                {"pages": [1, 3], "kind": "complaint", "date": "2025-01-01", "headline": "a"},
                {"pages": [3, 4], "kind": "notice", "date": "2025-01-02", "headline": "b"},
                {"pages": [7], "kind": "other", "headline": "c"},
            ]
        }
    )
    monkeypatch.setattr(fs.llm, "complete", lambda *a, **k: reply)
    spans = [(s.first, s.last) for s in fs.segment_pages(["p"] * 8)]
    assert spans == [(1, 3), (4, 4), (5, 6), (7, 7), (8, 8)]  # overlap clipped, gaps filled


def test_split_file_makes_a_dated_stage_and_document_for_each_part(
    monkeypatch, make_complaint, make_document, clerk
):
    c = make_complaint()
    source = make_document(c, kind="complaint")
    Document.objects.filter(pk=source.pk).update(
        mime_type="application/pdf", extracted_text="page one\n---\npage two", page_count=2
    )
    segments = [
        fs.Segment(1, 1, "complaint", date(2025, 10, 6), "A", "SDM", "Complaint about X", "s1"),
        fs.Segment(2, 2, "notice", date(2025, 10, 17), "SDM", "BDPO", "Report sought", "s2"),
    ]
    monkeypatch.setattr(fs, "segment_pages", lambda pages, **k: segments)
    monkeypatch.setattr(fs, "split_pdf", lambda content, a, b: f"%PDF-1.4 part {a}".encode())
    call_command("split_file", c.ref_no, actor=clerk.username, stdout=io.StringIO())
    stages = [e for e in c.events.order_by("occurred_at") if e.payload.get("document_ids")]
    assert [e.occurred_at.date() for e in stages] == [date(2025, 10, 6), date(2025, 10, 17)]
    assert stages[1].payload["headline"].startswith("SDM letter")
    c.refresh_from_db()
    assert c.received_on == date(2025, 10, 6)
    assert c.documents.filter(kind="notice", summary="s2").exists()
