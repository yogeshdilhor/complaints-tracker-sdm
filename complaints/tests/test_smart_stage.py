"""Typing "@office" and "by <date>" in a custom stage marks the complaint and sets the due date."""

from datetime import date

from django.urls import reverse

from complaints.models import Department, State
from complaints.workflow import Action, apply_transition

HX = {"HTTP_HX_REQUEST": "true"}


def post_stage(client, complaint, **fields):
    data = {"headline": "", "headline_own": "", "note": ""} | fields
    return client.post(reverse("complaints:action", args=[complaint.pk, "add_stage"]), data, **HX)


def test_the_form_offers_the_offices_for_at_mentions(client, clerk, make_complaint, tehsildar):
    client.force_login(clerk)
    c = make_complaint()
    html = client.get(reverse("complaints:action", args=[c.pk, "add_stage"]), **HX).content.decode()
    assert "data-smart" in html and "smart-departments-data" in html
    assert tehsildar.name in html and 'name="mark_department"' in html
    assert 'name="expected_by"' in html


def test_other_forms_have_no_smart_typing(client, clerk, make_complaint):
    client.force_login(clerk)
    c = make_complaint()
    html = client.get(reverse("complaints:action", args=[c.pk, "add_note"]), **HX).content.decode()
    assert "smart-departments-data" not in html and 'name="mark_department"' not in html


def test_an_at_mention_marks_a_new_complaint_with_the_due_date(
    client, clerk, make_complaint, tehsildar
):
    client.force_login(clerk)
    c = make_complaint()
    post_stage(
        client,
        c,
        headline="Report expected from Tehsildar by 25 Oct 2026",
        mark_department=tehsildar.pk,
        expected_by="2026-10-25",
    )
    c.refresh_from_db()
    assert c.current_state == State.AWAITING_REPORT and c.current_department == tehsildar
    referral = c.referrals.get()
    assert referral.due_on == date(2026, 10, 25)
    titles = [e.payload.get("headline") for e in c.events.all() if e.payload.get("headline")]
    assert "Report expected from Tehsildar by 25 Oct 2026" in titles


def test_a_new_office_can_be_typed(client, clerk, make_complaint, office):
    client.force_login(clerk)
    c = make_complaint()
    post_stage(
        client,
        c,
        headline="Report sought from Fire Officer",
        mark_new_department="Fire Officer",
    )
    c.refresh_from_db()
    created = Department.objects.get(office=office, name="Fire Officer")
    assert c.current_department == created and c.current_state == State.AWAITING_REPORT


def test_marking_again_while_waiting_moves_it_to_the_new_office(
    client, clerk, make_complaint, tehsildar, bdpo
):
    client.force_login(clerk)
    c = make_complaint()
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    post_stage(client, c, headline="Now with BDPO", mark_department=bdpo.pk)
    c.refresh_from_db()
    assert c.current_department == bdpo and c.current_state == State.AWAITING_REPORT


def test_without_a_mention_it_is_a_plain_stage(client, clerk, make_complaint):
    client.force_login(clerk)
    c = make_complaint()
    post_stage(client, c, headline="Phoned the complainant")
    c.refresh_from_db()
    assert c.current_state == State.RECEIVED and c.current_department is None


def test_the_at_sign_is_dropped_from_the_saved_title(client, clerk, make_complaint, office):
    client.force_login(clerk)
    c = make_complaint()
    post_stage(
        client,
        c,
        headline="Report received @ZCRIM to answer",
        mark_new_department="ZCRIM",
    )
    titles = [e.payload.get("headline") for e in c.events.all() if e.payload.get("headline")]
    assert "Report received ZCRIM to answer" in titles
