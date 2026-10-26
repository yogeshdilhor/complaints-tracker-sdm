"""Private details are masked out of what is sent to a model, and kept on the portal."""

import pytest

from complaints.services import description_summary as ds
from complaints.services.masking import mask_private


@pytest.mark.parametrize(
    "text, gone",
    [
        ("Aadhaar 4821 6093 7715", "4821"),
        ("call 98765 43210 now", "98765"),
        ("call +91-9876543210", "9876543210"),
        ("mail me at ram.lal@example.com", "example.com"),
        ("PAN ABCDE1234F", "ABCDE1234F"),
        ("account 123456789012", "123456789012"),
        ("फोन ९८७६५४३२१०", "९८७६५४३२१०"),
    ],
)
def test_id_phone_email_and_bank_numbers_are_masked(text, gone):
    assert gone not in mask_private(text)


def test_known_names_and_each_word_of_them_are_masked():
    out = mask_private("Ram Lal of Tohana says Lal was beaten", ["Ram Lal", "12 Gali Ram Nagar"])
    assert "Ram" not in out and "Lal" not in out and "Tohana" in out


def test_ordinary_text_and_short_numbers_are_left_alone():
    text = "The road in Tohana, 2 km long, needs repair by 10 October."
    assert mask_private(text) == text


def test_every_call_to_the_model_sends_masked_text(monkeypatch):
    sent = []
    monkeypatch.setattr(
        ds, "_complete", lambda prompt, **k: sent.append(prompt) or '{"summary": "s"}'
    )
    ds.extract_complaint_fields("Ram Lal 4821 6093 7715 9876543210 drain", [], known=["Ram Lal"])
    ds.summarise_report("Ram Lal 4821 6093 7715 drain", "Report", known=["Ram Lal"])
    ds.read_document_as_stage(
        "Ram Lal 9876543210",
        {"subject": "", "state": "", "department": "", "portal": ""},
        ["add_stage"],
        known=["Ram Lal"],
    )
    for prompt in sent:
        assert "Ram" not in prompt and "4821" not in prompt and "9876543210" not in prompt
    assert len(sent) == 3


def test_roles_are_not_masked_as_names():
    assert "Sarpanch" in mask_private(
        "The Sarpanch asks for repair", ["Sarpanch", "Multiple complainants"]
    )
