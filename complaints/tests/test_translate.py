"""Translating a complaint's summary: made once on the first click, then kept."""

from django.urls import reverse

from complaints.services import translate
from complaints.tests.test_views import login  # noqa: F401 - fixture


def test_detect_lang():
    assert translate.detect_lang("गांव बालुआना के किसानों की शिकायत है।") == "hi"
    assert translate.detect_lang("Farmers of Baluana village complain that") == "en"
    assert translate.detect_lang("") == "en"


def test_no_translate_button_without_a_description(login, clerk, make_complaint):  # noqa: F811
    c = make_complaint(description="")
    html = login(clerk).get(reverse("complaints:detail", args=[c.pk])).content.decode()
    assert "No summary recorded." in html
    assert "Translate" not in html


def test_translating_a_hindi_summary_asks_for_english_once_and_keeps_it(
    login,  # noqa: F811
    clerk,
    make_complaint,
    monkeypatch,
    settings,
):
    settings.OPENAI_API_KEY = "k"
    c = make_complaint(description="गांव बालुआना के किसानों की शिकायत है।")
    calls = []

    def fake_translate(text, to):
        calls.append((text, to))
        return "Farmers of Baluana village complain."

    monkeypatch.setattr(translate, "translate", fake_translate)
    client = login(clerk)

    html = client.post(reverse("complaints:complaint_translate", args=[c.pk])).content.decode()
    assert "Farmers of Baluana village complain." in html
    assert calls == [("गांव बालुआना के किसानों की शिकायत है।", "en")]
    c.refresh_from_db()
    assert c.description_translated == "Farmers of Baluana village complain."
    assert c.description_translated_lang == "en"

    # Asking again does not call the model a second time; the kept copy is reused.
    client.post(reverse("complaints:complaint_translate", args=[c.pk]))
    assert calls == [("गांव बालुआना के किसानों की शिकायत है।", "en")]


def test_an_english_summary_is_offered_a_hindi_translation(
    login,  # noqa: F811
    clerk,
    make_complaint,
    monkeypatch,
    settings,
):
    settings.OPENAI_API_KEY = "k"
    c = make_complaint(description="Farmers of Baluana village complain.")
    monkeypatch.setattr(translate, "translate", lambda text, to: f"[{to}] {text}")
    html = (
        login(clerk).post(reverse("complaints:complaint_translate", args=[c.pk])).content.decode()
    )
    assert "[hi] Farmers of Baluana village complain." in html


def test_a_failed_translation_leaves_the_button_to_try_again(
    login,  # noqa: F811
    clerk,
    make_complaint,
    monkeypatch,
    settings,
):
    settings.OPENAI_API_KEY = ""  # no key: translate.translate() would return None
    c = make_complaint(description="गांव बालुआना के किसानों की शिकायत है।")
    html = (
        login(clerk).post(reverse("complaints:complaint_translate", args=[c.pk])).content.decode()
    )
    assert ">Translate<" in html
    c.refresh_from_db()
    assert c.description_translated == ""
