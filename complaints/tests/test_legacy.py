"""Legacy complaints: a big scanned file becomes a complaint with its whole history."""

import json
from datetime import date

import pytest
from django.urls import reverse

from complaints.models import Complaint, LegacyImport, State
from complaints.services import file_splitter as fs
from complaints.services import legacy, llm, sarvam_docs
from complaints.tests.test_llm import FakeResponse
from complaints.tests.test_views import login, pdf  # noqa: F401,F811 - fixtures/helpers

PAGES = ["complaint email", "DC forwards", "SDM letter", "reminder"]
FIELDS = {
    "summary": "Staff misbehave at the Patwar Bhavan.",
    "subject": "Misbehaviour at Patwar Bhavan",
    "name": "Pawan Sharma",
    "phone": "9876543210",
    "address": "Goriwala",
    "village": "",
}


@pytest.fixture
def fake_ai(monkeypatch, settings):
    settings.OPENAI_API_KEY, settings.SARVAM_API_KEY = "k", "k"
    monkeypatch.setattr(legacy, "count_pages", lambda content: len(PAGES))
    monkeypatch.setattr(sarvam_docs, "digitise_pages", lambda content, mime, **k: list(PAGES))
    monkeypatch.setattr(
        fs,
        "split_pdf",
        lambda content, a, b: f"%PDF-1.4 pages {a}-{b}".encode(),
    )
    segments = [
        fs.Segment(4, 4, "reminder", date(2025, 11, 1), "SDM", "Naib Tehsildar", "Reminder", ""),
        fs.Segment(1, 1, "complaint", date(2025, 10, 6), "Pawan", "SDM", "Complaint", ""),
        fs.Segment(
            3, 3, "notice", date(2025, 10, 17), "SDM", "Naib Tehsildar", "Report sought", ""
        ),
        fs.Segment(2, 2, "forwarding", date(2025, 10, 13), "DC", "SDM", "Forwarded", ""),
    ]
    monkeypatch.setattr(fs, "segment_pages", lambda pages, provider="x", **k: segments)
    monkeypatch.setattr(legacy.llm, "complete", lambda *a, **k: "A fifty word summary.")
    monkeypatch.setattr(legacy, "extract_complaint_fields_unmasked", lambda *a, **k: dict(FIELDS))


def upload(client, **extra):
    data = {"file": pdf("old.pdf"), "reader": "sarvam", "analyser": "openai"} | extra
    return client.post(reverse("complaints:legacy_upload"), data)


def test_the_button_is_in_the_top_bar(login, clerk, office):  # noqa: F811
    html = login(clerk).get(reverse("complaints:list")).content.decode()
    assert "Legacy complaint" in html


def test_upload_reads_splits_and_waits_for_checking(login, clerk, fake_ai):  # noqa: F811
    client = login(clerk)
    response = upload(client)
    job = LegacyImport.objects.get()
    assert response.status_code == 302 and job.status == "ready" and job.pages == PAGES
    docs = job.proposal["documents"]
    assert [d["kind"] for d in docs] == ["complaint", "forwarding", "notice", "reminder"]  # by date
    assert all(d["summary"] == "A fifty word summary." for d in docs)
    assert Complaint.objects.count() == 0  # nothing is created before the check
    html = client.get(response["Location"]).content.decode()
    assert "Check the legacy complaint" in html and "Misbehaviour at Patwar Bhavan" in html


def test_confirming_creates_the_complaint_with_one_dated_stage_per_document(
    login,  # noqa: F811
    clerk,
    office,
    village,
    fake_ai,
    tehsildar,  # noqa: F811
):
    client = login(clerk)
    upload(client)
    job = LegacyImport.objects.get()
    data = {
        "name": "Pawan Sharma",
        "phone": "9876543210",
        "address": "",
        "village": village.pk,
        "subject": "Misbehaviour at Patwar Bhavan",
        "description": "Staff misbehave.",
        "source_platform": "internal",
        "external_ref": "",
        "received_on": "2025-10-06",
        "department": tehsildar.pk,
        "current_state": "awaiting",
        "closed_reason": "",
        "keep_0": "on",
        "keep_1": "on",
        "keep_2": "on",
        "headline_2": "Report sought from Naib Tehsildar",
        # document 3 (the reminder) is left unticked
    }
    response = client.post(reverse("complaints:legacy_status", args=[job.pk]), data)
    complaint = Complaint.objects.get()
    assert response.status_code == 302 and response["Location"].endswith(f"/c/{complaint.pk}/")
    assert complaint.received_on == date(2025, 10, 6)
    assert complaint.current_state == State.AWAITING_REPORT
    assert complaint.current_department == tehsildar
    stages = [e for e in complaint.events.order_by("occurred_at") if e.payload.get("document_ids")]
    assert [e.occurred_at.date() for e in stages] == [
        date(2025, 10, 6),
        date(2025, 10, 13),
        date(2025, 10, 17),
    ]
    assert stages[2].payload["headline"] == "SDM letter: Report sought from Naib Tehsildar"
    kinds = set(complaint.documents.values_list("kind", flat=True))
    assert {"complaint", "notice"} <= kinds and complaint.documents.count() == 4
    assert complaint.tags.filter(name="Legacy - verify").exists()
    job.refresh_from_db()
    assert job.status == "done" and job.complaint == complaint


def test_a_closed_legacy_complaint_needs_a_reason(login, clerk, village, fake_ai, tehsildar):  # noqa: F811
    client = login(clerk)
    upload(client)
    job = LegacyImport.objects.get()
    base = {
        "name": "A",
        "subject": "S",
        "source_platform": "internal",
        "received_on": "2025-10-06",
        "department": tehsildar.pk,
        "current_state": "closed",
        "keep_0": "on",
    }
    url = reverse("complaints:legacy_status", args=[job.pk])
    assert client.post(url, base).status_code == 200 and Complaint.objects.count() == 0
    done = client.post(url, base | {"closed_reason": "Disposed by the court"})
    assert done.status_code == 302 and Complaint.objects.get().current_state == State.CLOSED


def test_an_unreadable_file_fails_with_a_plain_message(login, clerk, fake_ai, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sarvam_docs, "digitise_pages", lambda *a, **k: None)
    client = login(clerk)
    response = upload(client)
    job = LegacyImport.objects.get()
    assert job.status == "failed" and "credit" in job.error
    assert "credit" in client.get(response["Location"]).content.decode()


def test_only_pdfs_and_configured_ais_are_accepted(login, clerk, fake_ai, settings):  # noqa: F811
    client = login(clerk)
    bad = client.post(
        reverse("complaints:legacy_upload"),
        {
            "file": pdf("x.pdf", b"")._set_name("x") or pdf(),
            "reader": "sarvam",
            "analyser": "anthropic",
        },
    )
    assert "no key" in bad.content.decode() and LegacyImport.objects.count() == 0


def test_claude_requests_use_the_messages_api(monkeypatch, settings):
    settings.ANTHROPIC_API_KEY, settings.ANTHROPIC_MODEL = "ak", "claude-sonnet-5-5"
    seen = {}

    class Resp(FakeResponse):
        def __init__(self):
            self.body = json.dumps(
                {"content": [{"type": "text", "text": '```json\n{"a": 1}\n```'}]}
            ).encode()

    def fake_urlopen(request, timeout=None):
        seen["url"], seen["headers"] = request.full_url, dict(request.header_items())
        seen["body"] = json.loads(request.data)
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = llm.complete("hi", max_tokens=100, as_json=True, provider_name="anthropic")
    assert out == '{"a": 1}'
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["X-api-key"] == "ak" and seen["body"]["max_tokens"] == 100
