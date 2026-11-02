"""Deleting a stage (SDM only, with a warning) and a subordinate office adding its response."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from complaints.models import Document, Event, EventType, State
from complaints.services.timeline import build_timeline
from complaints.tests.helpers import fake_attachment
from complaints.workflow import Action, add_stage, apply_transition, attach_document

HX = {"HTTP_HX_REQUEST": "true"}


def delete_url(complaint, event):
    return reverse("complaints:stage_delete", args=[complaint.pk, event.pk])


@pytest.fixture
def stage(complaint, clerk, make_document):
    document = make_document(complaint)
    event = add_stage(complaint, clerk, "Site visit", "Went there", [document])
    return event, document


def test_only_the_sdm_may_delete_a_stage(client, complaint, stage, reviewer, clerk, admin_role):
    event, _ = stage
    for user in (reviewer, clerk):
        client.force_login(user)
        assert client.get(delete_url(complaint, event), **HX).status_code == 403
        assert client.post(delete_url(complaint, event), {"reason": "x"}, **HX).status_code == 403
    assert Event.objects.filter(type=EventType.STAGE_DELETED).count() == 0
    client.force_login(admin_role)
    assert client.get(delete_url(complaint, event), **HX).status_code == 200


def test_the_sdm_is_warned_before_anything_is_deleted(client, complaint, stage, admin_role):
    event, _ = stage
    client.force_login(admin_role)
    html = client.get(delete_url(complaint, event), **HX).content.decode()
    assert "tampering with the record" in html and "go ahead" in html
    assert not Event.objects.filter(type=EventType.STAGE_DELETED).exists()


def test_confirming_hides_the_stage_but_leaves_a_record(client, complaint, stage, admin_role):
    event, document = stage
    client.force_login(admin_role)
    bad = client.post(delete_url(complaint, event), {"reason": ""}, **HX)
    assert "required" in bad.content.decode().lower()
    assert not Event.objects.filter(type=EventType.STAGE_DELETED).exists()

    ok = client.post(delete_url(complaint, event), {"reason": "Added to the wrong case"}, **HX)
    assert ok.status_code == 200 and ok["HX-Trigger"] == "complaintChanged"
    assert Event.objects.filter(pk=event.pk).exists()  # the original event is never erased
    document.refresh_from_db()
    assert document.is_deleted
    titles = [s.title for s in build_timeline(complaint)]
    assert "Site visit" not in titles
    assert "Stage deleted: Site visit" in titles
    record = Event.objects.get(type=EventType.STAGE_DELETED)
    assert record.actor == admin_role and record.payload["reason"] == "Added to the wrong case"
    # ...and it cannot be deleted twice.
    again = client.post(delete_url(complaint, event), {"reason": "again"}, **HX)
    assert "already deleted" in again.content.decode()


def test_a_stage_that_moved_the_case_cannot_be_deleted(
    client, complaint, clerk, tehsildar, admin_role
):
    apply_transition(complaint, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    notice = complaint.events.get(type=EventType.NOTICE_ISSUED)
    client.force_login(admin_role)
    html = client.post(delete_url(complaint, notice), {"reason": "x"}, **HX).content.decode()
    assert "cannot be deleted" in html
    assert not any(s.deletable for s in build_timeline(complaint) if s.title.startswith("Marked"))


def test_only_the_sdm_sees_the_delete_button(client, complaint, stage, admin_role, reviewer):
    url = reverse("complaints:detail", args=[complaint.pk])
    client.force_login(admin_role)
    assert "Delete this stage" in client.get(url).content.decode()
    client.force_login(reviewer)
    assert "Delete this stage" not in client.get(url).content.decode()


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
