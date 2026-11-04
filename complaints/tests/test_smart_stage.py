"""Adding a stage: marking it to a department or officer, with comments and a reply-by date."""

from datetime import date, timedelta

from django.urls import reverse
from django.utils import timezone

from complaints.models import Department, State
from complaints.workflow import Action, apply_transition

HX = {"HTTP_HX_REQUEST": "true"}


def post_stage(client, complaint, **fields):
    data = {"headline": "A stage", "note": ""} | fields
    return client.post(reverse("complaints:action", args=[complaint.pk, "add_stage"]), data, **HX)


def test_the_form_has_department_comments_and_a_reply_date(
    client, clerk, make_complaint, tehsildar
):
    client.force_login(clerk)
    c = make_complaint()
    html = client.get(reverse("complaints:action", args=[c.pk, "add_stage"]), **HX).content.decode()
    assert tehsildar.name in html
    for name in ("department", "note", "due_on", "files", "headline"):
        assert f'name="{name}"' in html, name
    assert "smart-departments-data" not in html  # the @ and # typing tricks are gone


def test_marking_a_new_complaint_sets_the_officer_and_due_date(
    client, clerk, make_complaint, tehsildar
):
    client.force_login(clerk)
    c = make_complaint()
    due = timezone.localdate() + timedelta(days=9)
    post_stage(
        client,
        c,
        headline="Report sought from Tehsildar",
        department=tehsildar.pk,
        due_on=due.isoformat(),
        note="Please check the khasra numbers.",
    )
    c.refresh_from_db()
    assert c.current_state == State.AWAITING_REPORT and c.current_department == tehsildar
    referral = c.referrals.get()
    assert referral.due_on == due
    titles = [e.payload.get("headline") for e in c.events.all() if e.payload.get("headline")]
    assert "Report sought from Tehsildar" in titles


def test_a_new_office_can_be_typed(client, clerk, make_complaint, office):
    client.force_login(clerk)
    c = make_complaint()
    post_stage(client, c, headline="Report sought from Fire Officer", department_own="Fire Officer")
    c.refresh_from_db()
    created = Department.objects.get(office=office, name="Fire Officer")
    assert c.current_department == created and c.current_state == State.AWAITING_REPORT


def test_marking_again_while_waiting_moves_it_to_the_new_office(
    client, clerk, make_complaint, tehsildar, bdpo
):
    client.force_login(clerk)
    c = make_complaint()
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    post_stage(client, c, headline="Now with BDPO", department=bdpo.pk)
    c.refresh_from_db()
    assert c.current_department == bdpo and c.current_state == State.AWAITING_REPORT


def test_without_a_department_it_is_a_plain_stage(client, clerk, make_complaint):
    client.force_login(clerk)
    c = make_complaint()
    post_stage(client, c, headline="Phoned the complainant", note="No answer")
    c.refresh_from_db()
    assert c.current_state == State.RECEIVED and c.current_department is None
    note = c.events.filter(payload__headline="Phoned the complainant").get()
    assert note.payload["note"] == "No answer"


def test_the_reply_date_cannot_be_in_the_past(client, clerk, make_complaint, tehsildar):
    client.force_login(clerk)
    c = make_complaint()
    html = post_stage(
        client, c, department=tehsildar.pk, due_on=date(2020, 1, 1).isoformat()
    ).content.decode()
    assert "cannot be in the past" in html
    c.refresh_from_db()
    assert c.current_state == State.RECEIVED


def test_a_clerk_cannot_mark_a_case_under_review_and_is_told_so(
    client, clerk, make_complaint, tehsildar, bdpo
):
    client.force_login(clerk)
    c = make_complaint()
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    apply_transition(c, Action.RECORD_REPORT, clerk)
    html = post_stage(client, c, department=bdpo.pk).content.decode()
    assert "not allowed to mark" in html
    c.refresh_from_db()
    assert c.current_state == State.UNDER_REVIEW and c.current_department is None


def test_an_approved_case_cannot_be_marked_from_here(
    client, reviewer, clerk, make_complaint, tehsildar, bdpo
):
    client.force_login(reviewer)
    c = make_complaint()
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    apply_transition(c, Action.RECORD_REPORT, clerk)
    apply_transition(c, Action.REVIEW_SATISFIED, reviewer, note="ok")
    html = post_stage(client, c, department=bdpo.pk).content.decode()
    assert "cannot be marked to an officer here" in html
