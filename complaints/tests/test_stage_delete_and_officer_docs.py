"""Deleting a stage (SDM only, with a warning) and a subordinate office adding its response."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from complaints.models import Document, Event, EventType, State
from complaints.services.timeline import build_timeline
from complaints.tests.helpers import fake_attachment
from complaints.workflow import Action, add_stage, apply_transition, attach_document

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


def test_change_marking_is_not_offered_once_the_case_is_under_review(
    client, complaint, clerk, reviewer, tehsildar, bdpo
):
    apply_transition(complaint, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    apply_transition(complaint, Action.RECORD_REPORT, clerk)
    client.force_login(reviewer)
    assert client.post(change_url(complaint), {"department": bdpo.pk}, **HX).status_code == 403


# ------------------------------------------------- a subordinate office adds its response


@pytest.fixture
def marked(complaint, clerk, tehsildar):
    apply_transition(complaint, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    return complaint


def test_an_officer_can_add_a_document_and_it_shows_on_their_timeline(client, marked, officer):
    client.force_login(officer)
    url = reverse("complaints:action", args=[marked.pk, "attach_document"])
    form = client.get(url, **HX).content.decode()
    assert 'name="file"' in form and 'name="kind"' not in form and 'name="supersedes"' not in form
    upload = SimpleUploadedFile("reply.pdf", b"%PDF-1.4 reply", content_type="application/pdf")
    ok = client.post(url, {"file": upload}, **HX)
    assert ok.status_code == 200
    doc = Document.objects.get(complaint=marked, uploaded_by=officer)
    assert marked.current_state == State.AWAITING_REPORT  # a document is not a report
    titles = [s.title for s in build_timeline(marked, department=officer.department)]
    assert any("Response from" in t for t in titles)
    assert client.get(reverse("complaints:document", args=[doc.pk])).status_code == 200


def test_an_officer_still_cannot_do_anything_else(client, marked, officer):
    client.force_login(officer)
    for action in ("add_note", "add_stage", "edit_details", "file_complaint", "issue_notice"):
        url = reverse("complaints:action", args=[marked.pk, action])
        assert client.post(url, {"text": "x", "headline": "x"}, **HX).status_code == 403, action


def test_documents_added_by_staff_stay_hidden_from_the_office(marked, officer, clerk):
    attach_document(marked, clerk, fake_attachment("internal"), "other", headline="Internal")
    titles = [s.title for s in build_timeline(marked, department=officer.department)]
    assert "Internal" not in titles
