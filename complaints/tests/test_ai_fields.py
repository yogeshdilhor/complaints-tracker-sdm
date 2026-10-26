"""Reading a letter with the model: field merging, JSON parsing, portal deadlines."""

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from complaints.models import Platform, Tag
from complaints.services import description_summary as ai
from complaints.services import intake
from complaints.workflow import Action, apply_transition


def test_extract_fields_parses_the_models_json(settings, monkeypatch):
    reply = json.dumps({"summary": "Drain blocked.", "name": "Ram", "tag": "drain"})
    monkeypatch.setattr(ai, "_complete", lambda *a, **k: reply)
    fields = ai.extract_complaint_fields("letter", ["Bhattu"])
    assert fields["summary"] == "Drain blocked." and fields["tag"] == "drain"
    assert fields["village"] == "" and "name" not in fields


@pytest.mark.parametrize("reply", [None, "not json", "[1, 2]"])
def test_extract_fields_gives_nothing_when_the_call_or_reply_is_bad(monkeypatch, reply):
    monkeypatch.setattr(ai, "_complete", lambda *a, **k: reply)
    assert ai.extract_complaint_fields("letter", []) == {}


def test_extract_fields_does_nothing_without_a_key(settings):
    settings.OPENAI_API_KEY, settings.LLM_PROVIDER = "", "openai"
    assert ai.extract_complaint_fields("letter", []) == {}


def test_merge_ai_fields_overrides_guesses_and_matches_village_and_tag(office, village):
    Tag.objects.create(office=office, name="Pension")
    prefill = intake.Prefill(name="wrong", phone="1", subject="kept")
    intake.merge_ai_fields(
        prefill,
        {
            "summary": "Short.",
            "name": "Sunita Devi",
            "phone": "+91 90000 12345",
            "village": village.name.upper(),
            "tag": "pension",
            "subject": "The model heading",
        },
        office,
    )
    assert (prefill.name, prefill.phone, prefill.description) == (
        "Sunita Devi",
        "9000012345",
        "Short.",
    )
    assert prefill.village_id == village.pk and prefill.subject == "The model heading"
    assert prefill.tag_id and prefill.new_tag == ""
    assert prefill.as_initial()["tags"] == [prefill.tag_id]


def test_merge_ai_fields_suggests_a_new_tag_and_ignores_a_bad_phone(office):
    prefill = intake.Prefill(phone="9111122222")
    intake.merge_ai_fields(prefill, {"phone": "12345", "tag": "village road encroachment"}, office)
    assert prefill.phone == "9111122222"
    assert prefill.new_tag == "village road encroachment" and prefill.tag_id is None


def test_merge_ai_fields_with_nothing_changes_nothing(office):
    prefill = intake.Prefill(name="x")
    intake.merge_ai_fields(prefill, {}, office)
    assert prefill == intake.Prefill(name="x")


def test_samadhan_shivir_gets_a_seven_day_deadline_but_never_a_longer_one(
    make_complaint, clerk, tehsildar
):
    tehsildar.default_sla_days = 15
    tehsildar.save()
    shivir = make_complaint(source_platform=Platform.SAMADHAN_SHIVIR)
    normal = make_complaint(source_platform=Platform.CM_WINDOW)
    apply_transition(shivir, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    apply_transition(normal, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    today = timezone.localdate()
    assert shivir.referrals.get().due_on == today + timedelta(days=7)
    assert normal.referrals.get().due_on == today + timedelta(days=15)

    tehsildar.default_sla_days = 3
    tehsildar.save()
    short = make_complaint(source_platform=Platform.SAMADHAN_SHIVIR)
    apply_transition(short, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    assert short.referrals.get().due_on == today + timedelta(days=3)
