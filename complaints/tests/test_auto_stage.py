"""Upload a document, let the model read it, confirm, and it becomes a stage."""

from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from complaints.models import Document, State
from complaints.services.documents import Extraction
from complaints.services.timeline import build_timeline
from complaints.workflow import Action, apply_transition

HX = {"HTTP_HX_REQUEST": "true"}
REPORT_TEXT = "Report of the BDPO: the drain was cleaned on 10 October and water now flows."


@pytest.fixture
def awaiting(make_complaint, clerk, tehsildar):
    c = make_complaint()
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    return c


@pytest.fixture
def ai(monkeypatch, settings):
    """A model that reads the document as a report from the Tehsildar."""
    settings.OPENAI_API_KEY, settings.LLM_PROVIDER = "k", "openai"
    seen = []

    def fake(text, context, allowed):
        seen.append((text, context, allowed))
        return {
            "action": "record_report",
            "headline": "Tehsildar report on drain filed",
            "officer": "Tehsildar Dabwali",
            "date": date.today().isoformat(),
            "summary": "The Tehsildar says the drain was cleaned and water flows again.",
            "ack_no": "",
            "reason": "",
            "note": "",
        }

    monkeypatch.setattr("complaints.views.read_document_as_stage", fake)
    monkeypatch.setattr(
        "complaints.views.extract",
        lambda content, mime: Extraction(REPORT_TEXT, "done", 2, typed=True),
    )
    return seen


def url(complaint):
    return reverse("complaints:stage_from_document", args=[complaint.pk])


def pdf(name="report.pdf", body=b"report body"):
    return SimpleUploadedFile(name, b"%PDF-1.4 " + body, "application/pdf")


def test_the_upload_form_is_offered(client, clerk, awaiting):
    client.force_login(clerk)
    html = client.get(url(awaiting), **HX).content.decode()
    assert "Upload a new document" in html and 'type="file"' in html


def test_the_plus_menu_has_the_upload_option(client, clerk, awaiting):
    client.force_login(clerk)
    html = client.get(reverse("complaints:detail", args=[awaiting.pk])).content.decode()
    assert "Upload a document" in html and "Read it automatically" in html


def test_reading_a_report_fills_in_the_stage_for_confirmation(client, clerk, awaiting, ai):
    client.force_login(clerk)
    html = client.post(url(awaiting), {"file": pdf()}, **HX).content.decode()
    assert "Check what was read" in html and "report.pdf" in html
    assert "The Tehsildar says the drain was cleaned" in html  # the editable summary
    assert "Tehsildar report on drain filed" in html  # the proposed title
    assert 'name="staged"' in html
    assert 'value="record_report" selected' in html.replace("\n", " ") or "selected" in html
    text, context, allowed = ai[0]
    assert text == REPORT_TEXT and context["state"] == "Awaiting report"
    assert "record_report" in allowed and "add_stage" in allowed
    awaiting.refresh_from_db()
    assert awaiting.current_state == State.AWAITING_REPORT  # nothing is logged yet


def test_confirming_logs_the_stage_with_the_document_and_its_summary(client, clerk, awaiting, ai):
    client.force_login(clerk)
    page = client.post(url(awaiting), {"file": pdf()}, **HX).content.decode()
    sha = page.split('name="staged" value="')[1].split('"')[0]
    response = client.post(
        reverse("complaints:action", args=[awaiting.pk, "record_report"]),
        {
            "staged": sha,
            "doc_summary": "My edited summary.",
            "headline": "Tehsildar report on drain filed",
        },
        **HX,
    )
    assert response.status_code == 200
    awaiting.refresh_from_db()
    assert awaiting.current_state == State.UNDER_REVIEW
    stage = build_timeline(awaiting)[-1]
    assert stage.title == "Tehsildar report on drain filed"
    document = stage.documents[0]
    assert document.original_filename == "report.pdf"
    assert document.kind == Document.Kind.INQUIRY_REPORT
    assert document.summary == "My edited summary."
    assert document.extracted_text == REPORT_TEXT and document.is_typed is True


def test_the_stage_type_can_be_changed_before_confirming(client, clerk, awaiting, ai):
    client.force_login(clerk)
    client.post(url(awaiting), {"file": pdf()}, **HX)
    html = client.get(url(awaiting), {"type": "add_stage"}, **HX).content.decode()
    assert "Name this stage" in html and "Check what was read" in html
    # a type that is not valid for this case falls back to a custom stage
    html = client.get(url(awaiting), {"type": "reopen"}, **HX).content.decode()
    assert "Name this stage" in html


def test_a_custom_stage_from_a_document(client, clerk, awaiting, ai):
    client.force_login(clerk)
    page = client.post(url(awaiting), {"file": pdf()}, **HX).content.decode()
    sha = page.split('name="staged" value="')[1].split('"')[0]
    client.post(
        reverse("complaints:action", args=[awaiting.pk, "add_stage"]),
        {"staged": sha, "doc_summary": "s", "headline": "Letter from the village", "note": "n"},
        **HX,
    )
    stage = build_timeline(awaiting)[-1]
    assert stage.title == "Letter from the village" and len(stage.documents) == 1
    assert stage.documents[0].summary == "s"


def test_a_handwritten_document_is_not_sent_to_the_model(
    client, clerk, awaiting, monkeypatch, settings
):
    settings.OPENAI_API_KEY = "k"
    calls = []
    monkeypatch.setattr("complaints.views.read_document_as_stage", lambda *a: calls.append(a) or {})
    monkeypatch.setattr(
        "complaints.views.extract",
        lambda content, mime: Extraction("scrawl", "done", 1, typed=False),
    )
    client.force_login(clerk)
    html = client.post(url(awaiting), {"file": pdf()}, **HX).content.decode()
    assert not calls and "could not be read automatically" in html
    assert "Name this stage" in html  # falls back to a custom stage to fill in by hand


def test_without_a_key_the_form_still_works_by_hand(client, clerk, awaiting, monkeypatch, settings):
    settings.OPENAI_API_KEY = ""
    monkeypatch.setattr(
        "complaints.views.extract", lambda content, mime: Extraction("typed", "done", 1, typed=True)
    )
    client.force_login(clerk)
    html = client.post(url(awaiting), {"file": pdf()}, **HX).content.decode()
    assert "Check what was read" in html and "Filled in from the document" not in html


def test_the_confirmation_remembers_the_departments_the_document_names(
    client, clerk, make_complaint, tehsildar, ai, monkeypatch
):
    c = make_complaint()  # still RECEIVED: a notice marks it to someone
    monkeypatch.setattr(
        "complaints.views.read_document_as_stage",
        lambda *a: {
            "action": "issue_notice",
            "headline": "Marked to the Tehsildar",
            "officer": "Tehsildar office, Dabwali",
            "date": "",
            "summary": "s",
            "ack_no": "",
            "reason": "",
            "note": "Please report in a week",
        },
    )
    client.force_login(clerk)
    html = client.post(url(c), {"file": pdf()}, **HX).content.decode()
    # the officer wheel opens on the Tehsildar, not on "Choose..."
    wheel = html.split('data-name="department"')[1].split("</ul>")[0]
    assert 'class="selected">Tehsildar<' in wheel
    assert "Please report in a week" in html


def test_an_expired_or_missing_document_is_refused(client, clerk, awaiting):
    client.force_login(clerk)
    response = client.post(
        reverse("complaints:action", args=[awaiting.pk, "record_report"]),
        {"staged": "0" * 64, "headline": "x"},
        **HX,
    )
    assert "expired" in response.content.decode()
    awaiting.refresh_from_db()
    assert awaiting.current_state == State.AWAITING_REPORT


def test_a_document_already_on_the_case_is_not_added_twice(client, clerk, awaiting, ai):
    client.force_login(clerk)
    page = client.post(url(awaiting), {"file": pdf()}, **HX).content.decode()
    sha = page.split('name="staged" value="')[1].split('"')[0]
    action = reverse("complaints:action", args=[awaiting.pk, "add_stage"])
    client.post(action, {"staged": sha, "headline": "One"}, **HX)
    again = client.post(url(awaiting), {"file": pdf()}, **HX).content.decode()
    sha2 = again.split('name="staged" value="')[1].split('"')[0]
    response = client.post(action, {"staged": sha2, "headline": "Two"}, **HX)
    assert "already attached" in response.content.decode()
    assert awaiting.documents.count() == 1  # only the first upload


def test_officers_cannot_use_it(client, officer, awaiting):
    client.force_login(officer)
    assert client.get(url(awaiting), **HX).status_code == 403
    assert client.post(url(awaiting), {"file": pdf()}, **HX).status_code == 403


def test_the_model_reply_is_cleaned(monkeypatch, settings):
    import json

    from complaints.services import description_summary as ds

    settings.OPENAI_API_KEY = "k"
    monkeypatch.setattr(
        ds, "_complete", lambda *a, **k: json.dumps({"action": "fly_to_moon", "summary": " s "})
    )
    result = ds.read_document_as_stage(
        "text", {"subject": "x", "state": "y", "department": "z", "portal": "p"}, ["record_report"]
    )
    assert result["action"] == "add_stage" and result["summary"] == "s"
    monkeypatch.setattr(ds, "_complete", lambda *a, **k: "not json")
    assert (
        ds.read_document_as_stage(
            "t", {"subject": "", "state": "", "department": "", "portal": ""}, []
        )
        == {}
    )
