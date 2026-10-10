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


def test_handwritten_letters_are_not_sent_to_the_model(login, clerk, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(
        "complaints.views.extract",
        lambda content, mime: Extraction("scrawl", "done", 1, typed=False),
    )
    monkeypatch.setattr(
        "complaints.views.extract_complaint_fields", lambda *a: calls.append(a) or {"name": "X"}
    )
    client = login(clerk)
    upload_step(client)
    html = client.get(reverse("complaints:intake_review")).content.decode()
    assert not calls and "handwritten" in html


def test_typed_letters_are_sent_to_the_model(login, clerk, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(
        "complaints.views.extract",
        lambda content, mime: Extraction("A typed letter", "done", 1, typed=True),
    )
    monkeypatch.setattr(
        "complaints.views.extract_complaint_fields", lambda *a: calls.append(a) or {}
    )
    client = login(clerk)
    upload_step(client)
    client.get(reverse("complaints:intake_review"))
    assert len(calls) == 1


def test_a_handwritten_document_gets_no_summary(login, clerk, complaint, make_document, settings):  # noqa: F811
    settings.OPENAI_API_KEY = "k"
    doc = make_document(complaint)
    doc.extracted_text, doc.is_typed = "scrawl", False
    doc.save()
    html = login(clerk).post(reverse("complaints:document_summary", args=[doc.pk])).content.decode()
    assert "handwritten" in html
    doc.refresh_from_db()
    assert doc.summary == ""
