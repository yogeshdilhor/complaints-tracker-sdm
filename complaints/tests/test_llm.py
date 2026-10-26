"""Choosing the provider, and reading only typed documents."""

import json

import pytest
from django.urls import reverse

from complaints.services import description_summary as ai
from complaints.services import llm
from complaints.services.documents import Extraction, extract
from complaints.tests.test_intake import tools  # noqa: F401 - fake pdftotext/tesseract fixture
from complaints.tests.test_views import login, upload_step  # noqa: F401 - fixtures/helpers


class FakeResponse:
    def __init__(self, text="ok"):
        self.body = json.dumps({"choices": [{"message": {"content": text}}]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *a):
        return self.body


@pytest.fixture
def sent(monkeypatch):
    """Capture the HTTP request the app would make."""
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data)
        return FakeResponse("a summary")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return captured


def test_openai_provider_request(settings, sent):
    settings.LLM_PROVIDER, settings.OPENAI_API_KEY = "openai", "key-o"
    settings.OPENAI_MODEL, settings.OPENAI_REASONING_EFFORT = "gpt-6-luna", ""
    assert llm.complete("hi", max_tokens=300, as_json=True) == "a summary"
    assert sent["url"] == "https://api.openai.com/v1/chat/completions"
    assert sent["headers"]["authorization"] == "Bearer key-o"
    body = sent["body"]
    assert body["model"] == "gpt-6-luna" and body["max_completion_tokens"] == 300
    assert "max_tokens" not in body and "reasoning_effort" not in body
    assert body["response_format"] == {"type": "json_object"}


def test_sarvam_provider_request_turns_thinking_off(settings, sent):
    settings.LLM_PROVIDER, settings.SARVAM_API_KEY = "sarvam", "key-s"
    settings.SARVAM_MODEL, settings.SARVAM_REASONING_EFFORT = "sarvam-105b", "off"
    llm.complete("hi", max_tokens=300)
    assert sent["url"] == "https://api.sarvam.ai/v1/chat/completions"
    assert sent["headers"]["authorization"] == "Bearer key-s"
    body = sent["body"]
    assert body["model"] == "sarvam-105b" and body["max_tokens"] == 300
    assert "max_completion_tokens" not in body and body["reasoning_effort"] is None


def test_a_reasoning_level_leaves_room_for_the_thinking_tokens(settings, sent):
    settings.LLM_PROVIDER, settings.OPENAI_API_KEY = "openai", "k"
    settings.OPENAI_REASONING_EFFORT = "low"
    llm.complete("hi", max_tokens=300)
    assert sent["body"]["reasoning_effort"] == "low"
    assert sent["body"]["max_completion_tokens"] == 300 + llm.REASONING_HEADROOM


def test_only_the_chosen_providers_key_counts(settings):
    settings.OPENAI_API_KEY, settings.SARVAM_API_KEY = "k", ""
    settings.LLM_PROVIDER = "openai"
    assert llm.is_configured()
    settings.LLM_PROVIDER = "sarvam"
    assert not llm.is_configured() and llm.complete("hi", max_tokens=10) is None


def test_a_failed_call_gives_none(settings, monkeypatch):
    settings.LLM_PROVIDER, settings.OPENAI_API_KEY = "openai", "k"

    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert llm.complete("hi", max_tokens=10) is None
    assert ai.summarise_report("some text", "Inquiry report") is None


# ------------------------------------------------------------ typed only


def test_a_text_layer_pdf_is_typed(tools):  # noqa: F811
    tools("A typed complaint with enough text.\f")
    assert extract(b"%PDF-1.4", "application/pdf").typed is True


def test_a_clearly_read_scan_is_typed_and_a_poorly_read_one_is_handwriting(tools):  # noqa: F811
    tools("\f", hindi="Printed words", hindi_conf=88)
    assert extract(b"%PDF-1.4", "application/pdf").typed is True
    tools("\f", hindi="shaky scrawl", hindi_conf=42, latin_conf=30)
    assert extract(b"%PDF-1.4", "application/pdf").typed is False


def test_one_handwritten_page_in_a_mixed_pdf_makes_it_handwritten(tools):  # noqa: F811
    tools("A typed first page with enough text.\f\f", hindi="scrawl", hindi_conf=40, latin_conf=30)
    assert extract(b"%PDF-1.4", "application/pdf").typed is False


def test_handwritten_letters_are_still_sent_but_flagged(login, clerk, monkeypatch, settings):  # noqa: F811
    settings.OPENAI_API_KEY = "k"
    calls = []
    monkeypatch.setattr(
        "complaints.views.extract",
        lambda content, mime: Extraction("scrawl", "done", 1, typed=False),
    )
    monkeypatch.setattr(
        "complaints.views.extract_complaint_fields",
        lambda *a, **k: calls.append(a) or {},
    )
    client = login(clerk)
    upload_step(client)
    html = client.get(reverse("complaints:intake_review")).content.decode()
    assert len(calls) == 1 and "handwritten" in html


def test_typed_letters_are_sent_once_to_the_chosen_provider(login, clerk, monkeypatch, settings):  # noqa: F811
    settings.SARVAM_API_KEY = "k"
    calls = []
    monkeypatch.setattr(
        "complaints.views.extract",
        lambda content, mime: Extraction("A typed letter", "done", 1, typed=True),
    )
    monkeypatch.setattr(
        "complaints.views.extract_complaint_fields",
        lambda *a, **k: calls.append(k.get("provider")) or {},
    )
    client = login(clerk)
    upload_step(client, ai_provider="sarvam")
    client.get(reverse("complaints:intake_review"))
    assert calls == ["sarvam"]


def test_choosing_no_ai_sends_nothing(login, clerk, monkeypatch, settings):  # noqa: F811
    settings.OPENAI_API_KEY = "k"
    calls = []
    monkeypatch.setattr(
        "complaints.views.extract",
        lambda content, mime: Extraction("A typed letter", "done", 1, typed=True),
    )
    monkeypatch.setattr(
        "complaints.views.extract_complaint_fields", lambda *a, **k: calls.append(a) or {}
    )
    upload_step(login(clerk), ai_provider="none")
    assert not calls


def test_a_handwritten_document_gets_no_summary(login, clerk, complaint, make_document, settings):  # noqa: F811
    settings.OPENAI_API_KEY = "k"
    doc = make_document(complaint)
    doc.extracted_text, doc.is_typed = "scrawl", False
    doc.save()
    html = login(clerk).post(reverse("complaints:document_summary", args=[doc.pk])).content.decode()
    assert "handwritten" in html
    doc.refresh_from_db()
    assert doc.summary == ""


def test_vision_upload_sends_page_images_to_openai_and_prefills(
    login,  # noqa: F811
    clerk,
    monkeypatch,
    settings,  # noqa: F811
):
    settings.OPENAI_API_KEY = "k"
    seen = {}

    def fake_images(prompt, images, **kw):
        seen["images"], seen["prompt"] = images, prompt
        return json.dumps(
            {
                "summary": "Tower wire burns.",
                "subject": "Tower wire",
                "name": "Sarpanch, Gram Panchayat Naurang",
            }
        )

    monkeypatch.setattr(llm, "complete_with_images", fake_images)
    monkeypatch.setattr("complaints.views.page_images", lambda content, mime: [b"\xff\xd8\xffjpg"])
    monkeypatch.setattr(
        "complaints.views.extract", lambda content, mime: Extraction("", "done", 1, typed=False)
    )
    client = login(clerk)
    upload_step(client, ai_provider="openai_vision")
    html = client.get(reverse("complaints:intake_review")).content.decode()
    assert seen["images"] == [b"\xff\xd8\xffjpg"] and "Sarpanch" in seen["prompt"]
    assert "Sarpanch, Gram Panchayat Naurang" in html and "Tower wire burns." in html
    assert "looks <strong>handwritten</strong>" not in html


def test_vision_request_carries_the_image_as_a_data_url(monkeypatch, settings, sent):
    settings.OPENAI_API_KEY, settings.OPENAI_VISION_MODEL = "k", "gpt-4.1-mini"
    llm.complete_with_images("read", [b"\xff\xd8\xffjpg"], max_tokens=50)
    body = sent["body"]
    content = body["messages"][0]["content"]
    assert body["model"] == "gpt-4.1-mini"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


LETTER = "सुखपाल सिह पुत्र हरनेक सिह निवासी गांव फुल्लो\nमो० 87088-54538"
FIELDS = {
    "summary": "Rubble piled on the government road in front of the house.",
    "subject": "Rubble on public road at Phullo",
    "name": "सुखपाल सिह",
    "phone": "8708854538",
    "address": "गांव फुल्लो",
    "village": "फुल्लो",
    "tag": "road obstruction",
}


def test_sarvam_pages_upload_fills_every_field(login, clerk, monkeypatch, settings):  # noqa: F811
    from complaints.services import sarvam_docs

    settings.SARVAM_API_KEY = "k"
    monkeypatch.setattr(sarvam_docs, "digitise", lambda content, mime, **k: LETTER)
    monkeypatch.setattr(
        "complaints.views.extract_complaint_fields_unmasked", lambda *a, **k: dict(FIELDS)
    )
    monkeypatch.setattr(
        "complaints.views.extract", lambda content, mime: Extraction("", "done", 1, typed=False)
    )
    client = login(clerk)
    upload_step(client, ai_provider="sarvam_docs")
    html = client.get(reverse("complaints:intake_review")).content.decode()
    assert "सुखपाल सिह" in html and "Rubble on public road at Phullo" in html
    assert "8708854538" in html and "Rubble piled on the government road" in html
    assert "looks <strong>handwritten</strong>" not in html


def test_digitise_returns_the_text_of_each_page(monkeypatch, settings):
    import io
    import zipfile

    from complaints.services import sarvam_docs

    settings.SARVAM_API_KEY = "k"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(
            "letter.pdf/metadata/page_001.json",
            json.dumps({"blocks": [{"reading_order": 1, "text": "बिजली की समस्या"}]}),
        )
    calls = iter(
        [
            {"job_id": "j1", "status": "pending"},
            {"status": "completed"},
            {"url": "https://download.example/x.zip"},
        ]
    )
    monkeypatch.setattr(sarvam_docs, "_call", lambda *a, **k: next(calls))
    monkeypatch.setattr(sarvam_docs.time, "sleep", lambda s: None)

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Resp(archive.getvalue()))
    assert sarvam_docs.digitise(b"%PDF", "application/pdf") == "बिजली की समस्या"


def test_reread_digitise_updates_name_heading_summary_and_text(
    monkeypatch,
    settings,
    make_complaint,
    make_document,  # noqa: F811
):
    import io

    from django.core.management import call_command

    from complaints.models import Tag
    from complaints.services import sarvam_docs

    settings.SARVAM_API_KEY = "k"
    c = make_complaint()
    c.complainant.name = "Unknown"
    c.complainant.save()
    make_document(c, kind="complaint")
    Tag.objects.get_or_create(office=c.office, name="Imported - verify")[0].complaints.add(c)
    monkeypatch.setattr(sarvam_docs, "digitise", lambda content, mime, **k: LETTER)
    monkeypatch.setattr(
        "complaints.management.commands.reread_complaints.extract_complaint_fields_unmasked",
        lambda *a, **k: dict(FIELDS),
    )
    call_command("reread_complaints", "--digitise", stdout=io.StringIO())
    c = type(c).objects.select_related("complainant").get(pk=c.pk)
    assert c.complainant.name == "सुखपाल सिह" and c.subject == FIELDS["subject"]
    assert c.description == FIELDS["summary"]
    doc = c.documents.first()
    assert doc.extracted_text == LETTER and doc.summary == FIELDS["summary"]
