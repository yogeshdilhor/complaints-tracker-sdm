"""Deleting a stage (SDM only, with a warning) and a subordinate office adding its response."""

import pytest
from django.urls import reverse

from complaints.models import Event, EventType, State
from complaints.services.timeline import build_timeline
from complaints.workflow import Action, add_stage, apply_transition

HX = {"HTTP_HX_REQUEST": "true"}


def delete_url(complaint):
    return reverse("complaints:stage_delete", args=[complaint.pk])


@pytest.fixture
def stage(complaint, clerk, make_document):
    document = make_document(complaint)
    event = add_stage(complaint, clerk, "Site visit", "Went there", [document])
    return event, document


def test_the_sdm_and_reviewer_may_delete_a_stage_but_not_a_clerk(
    client, complaint, stage, reviewer, clerk, admin_role
):
    event, _ = stage
    client.force_login(clerk)
    assert client.get(delete_url(complaint), **HX).status_code == 403
    post = {"event_id": event.pk, "reason": "x"}
    assert client.post(delete_url(complaint), post, **HX).status_code == 403
    assert Event.objects.filter(type=EventType.STAGE_DELETED).count() == 0
    for user in (admin_role, reviewer):
        client.force_login(user)
        assert client.get(delete_url(complaint), **HX).status_code == 200


def test_the_form_lists_deletable_stages_and_warns_first(client, complaint, stage, admin_role):
    client.force_login(admin_role)
    html = client.get(delete_url(complaint), **HX).content.decode()
    assert "tampering with the record" in html and "go ahead" in html
    assert "Site visit" in html and "Complaint received" not in html  # not deletable
    assert 'hx-confirm="' in html
    assert not Event.objects.filter(type=EventType.STAGE_DELETED).exists()


def test_confirming_hides_the_stage_but_leaves_a_record(client, complaint, stage, reviewer):
    event, document = stage
    client.force_login(reviewer)
    bad = client.post(delete_url(complaint), {"event_id": event.pk, "reason": ""}, **HX)
    assert "required" in bad.content.decode().lower()
    assert not Event.objects.filter(type=EventType.STAGE_DELETED).exists()

    post = {"event_id": event.pk, "reason": "Added to the wrong case"}
    ok = client.post(delete_url(complaint), post, **HX)
    assert ok.status_code == 200 and ok["HX-Trigger"] == "complaintChanged"
    assert Event.objects.filter(pk=event.pk).exists()  # the original event is never erased
    document.refresh_from_db()
    assert document.is_deleted
    titles = [s.title for s in build_timeline(complaint)]
    assert "Site visit" not in titles
    assert "Stage deleted: Site visit" in titles
    record = Event.objects.get(type=EventType.STAGE_DELETED)
    assert record.actor == reviewer and record.payload["reason"] == "Added to the wrong case"
    # it is no longer on offer, and cannot be deleted twice
    again = client.post(delete_url(complaint), post, **HX)
    assert "no stage that can be deleted" in again.content.decode()
    assert Event.objects.filter(type=EventType.STAGE_DELETED).count() == 1


def test_a_stage_that_moved_the_case_cannot_be_deleted(
    client, complaint, clerk, tehsildar, admin_role
):
    apply_transition(complaint, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    notice = complaint.events.get(type=EventType.NOTICE_ISSUED)
    client.force_login(admin_role)
    html = client.get(delete_url(complaint), **HX).content.decode()
    assert "no stage that can be deleted" in html
    bad = client.post(delete_url(complaint), {"event_id": notice.pk, "reason": "x"}, **HX)
    assert not Event.objects.filter(type=EventType.STAGE_DELETED).exists()
    assert bad.status_code == 200


def test_delete_lives_in_the_edit_stage_menu_not_on_each_stage(
    client, complaint, stage, admin_role, reviewer, clerk
):
    url = reverse("complaints:detail", args=[complaint.pk])
    for user in (admin_role, reviewer):
        client.force_login(user)
        html = client.get(url).content.decode()
        assert "Edit stage" in html and delete_url(complaint) in html
        assert "Delete this stage" not in html
    client.force_login(clerk)
    html = client.get(url).content.decode()
    assert "Edit stage" not in html and delete_url(complaint) not in html
    assert "Add stage" in html


# ---------------------------------------------- changing the department or officer


def change_url(complaint):
    return reverse("complaints:action", args=[complaint.pk, "change_marking"])


def test_change_marking_moves_an_already_marked_complaint(
    client, complaint, clerk, reviewer, tehsildar, bdpo
):
    apply_transition(complaint, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    client.force_login(reviewer)
    ok = client.post(change_url(complaint), {"department": bdpo.pk, "note": "Wrong office"}, **HX)
    assert ok.status_code == 200 and ok["HX-Trigger"] == "complaintChanged"
    complaint.refresh_from_db()
    assert complaint.current_department == bdpo and complaint.current_state == State.AWAITING_REPORT
    assert complaint.referrals.count() == 2  # the old one is kept, as superseded
    titles = [s.title for s in build_timeline(complaint)]
    assert any("Marked to BDPO" in t for t in titles)


def test_change_marking_can_add_a_new_department_and_officer(
    client, complaint, clerk, reviewer, tehsildar, office
):
    from core.models import ConcernedDepartment

    apply_transition(complaint, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    client.force_login(reviewer)
    post = {"concerned_department_own": "Fire", "department_own": "Fire Officer Dabwali"}
    client.post(change_url(complaint), post, **HX)
    complaint.refresh_from_db()
    assert complaint.current_department.name == "Fire Officer Dabwali"
    assert complaint.current_department.concerned_department.name == "Fire"
    assert ConcernedDepartment.objects.filter(office=office, name="Fire").count() == 1


def test_change_marking_needs_a_department_and_the_right_role(
    client, complaint, clerk, reviewer, tehsildar, bdpo
):
    client.force_login(clerk)
    assert client.post(change_url(complaint), {"department": bdpo.pk}, **HX).status_code == 403
    client.force_login(reviewer)
    empty = client.post(change_url(complaint), {}, **HX)
    assert "Choose who this goes to" in empty.content.decode()
    complaint.refresh_from_db()
    assert complaint.current_department is None


def test_a_reply_that_says_not_ours_can_be_passed_to_another_department(
    client, complaint, clerk, reviewer, tehsildar, bdpo
):
    """A report comes in saying the matter belongs to someone else: mark it there."""
    apply_transition(complaint, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    apply_transition(complaint, Action.RECORD_REPORT, clerk)
    complaint.refresh_from_db()
    assert complaint.current_state == State.UNDER_REVIEW
    client.force_login(reviewer)
    ok = client.post(
        change_url(complaint), {"department": bdpo.pk, "note": "Tehsildar: not our subject"}, **HX
    )
    assert ok.status_code == 200 and ok["HX-Trigger"] == "complaintChanged"
    complaint.refresh_from_db()
    assert complaint.current_department == bdpo and complaint.current_state == State.AWAITING_REPORT
    titles = [s.title for s in build_timeline(complaint)]
    assert "Marked to BDPO instead" in titles
    assert any("not our subject" in s.detail for s in build_timeline(complaint))
    # a blank comment still works, with a default note
    apply_transition(complaint, Action.RECORD_REPORT, clerk)
    client.force_login(reviewer)
    assert client.post(change_url(complaint), {"department": tehsildar.pk}, **HX).status_code == 200
    complaint.refresh_from_db()
    assert complaint.current_department == tehsildar


def test_change_marking_is_not_offered_once_the_case_is_approved(
    client, complaint, clerk, reviewer, tehsildar, bdpo
):
    apply_transition(complaint, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    apply_transition(complaint, Action.RECORD_REPORT, clerk)
    apply_transition(complaint, Action.REVIEW_SATISFIED, reviewer, note="ok")
    client.force_login(reviewer)
    assert client.post(change_url(complaint), {"department": bdpo.pk}, **HX).status_code == 403
