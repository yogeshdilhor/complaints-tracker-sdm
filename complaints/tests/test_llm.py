"""Every call goes to gpt-6-luna, and only typed documents are read."""

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
    settings.OPENAI_API_KEY, settings.OPENAI_REASONING_EFFORT = "key-o", ""
    assert llm.complete("hi", max_tokens=300, as_json=True) == "a summary"
    assert sent["url"] == "https://api.openai.com/v1/chat/completions"
    assert sent["headers"]["authorization"] == "Bearer key-o"
    body = sent["body"]
    assert body["model"] == "gpt-6-luna" and body["max_completion_tokens"] == 300
    assert "max_tokens" not in body and "reasoning_effort" not in body
    assert body["response_format"] == {"type": "json_object"}


def test_a_reasoning_level_leaves_room_for_the_thinking_tokens(settings, sent):
    settings.OPENAI_API_KEY = "k"
    settings.OPENAI_REASONING_EFFORT = "low"
    llm.complete("hi", max_tokens=300)
    assert sent["body"]["reasoning_effort"] == "low"
    assert sent["body"]["max_completion_tokens"] == 300 + llm.REASONING_HEADROOM


def test_only_the_openai_key_counts(settings):
    settings.OPENAI_API_KEY = ""
    assert not llm.is_configured() and llm.complete("hi", max_tokens=10) is None
    settings.OPENAI_API_KEY = "k"
    assert llm.is_configured()


def test_a_failed_call_gives_none(settings, monkeypatch):
    settings.OPENAI_API_KEY = "k"

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


def test_typed_letters_are_sent_once_to_the_model(login, clerk, monkeypatch, settings):  # noqa: F811
    settings.OPENAI_API_KEY = "k"
    calls = []
    monkeypatch.setattr(
        "complaints.views.extract",
        lambda content, mime: Extraction("A typed letter", "done", 1, typed=True),
    )
    monkeypatch.setattr(
        "complaints.views.extract_complaint_fields",
        lambda *a, **k: calls.append(1) or {},
    )
    client = login(clerk)
    upload_step(client, ai_provider="openai")
    client.get(reverse("complaints:intake_review"))
    assert calls == [1]


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
    settings.OPENAI_API_KEY = "k"
    llm.complete_with_images("read", [b"\xff\xd8\xffjpg"], max_tokens=50)
    body = sent["body"]
    content = body["messages"][0]["content"]
    assert body["model"] == "gpt-6-luna"
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
