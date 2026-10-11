"""The notification centre: who is told what, the bell, the page, and reading them."""

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from complaints.models import Notification, Platform
from complaints.services import notifications as service
from complaints.workflow import Action, apply_transition

HX = {"HTTP_HX_REQUEST": "true"}


def mine(user):
    return list(Notification.objects.filter(recipient=user))


@pytest.fixture
def awaiting(make_complaint, clerk, tehsildar):
    c = make_complaint()
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    return c


# ------------------------------------------------------------------ who is told


def test_marking_a_case_tells_that_departments_officers_only(
    make_complaint, clerk, reviewer, officer, bdpo, tehsildar
):
    c = make_complaint()
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    [note] = mine(officer)  # the officer belongs to the Tehsildar
    assert note.title == "A complaint has been marked to you"
    assert c.ref_no in note.body and "Report due by" in note.body
    assert mine(clerk) == [] and mine(reviewer) == []  # the clerk did it; the reviewer is not told


def test_marking_to_another_department_does_not_tell_this_officer(
    make_complaint, clerk, officer, bdpo
):
    apply_transition(make_complaint(), Action.ISSUE_NOTICE, clerk, department=bdpo)
    assert mine(officer) == []


def test_a_filed_report_tells_the_reviewers_to_review_it(
    awaiting, officer, reviewer, clerk, office
):
    from complaints.tests.conftest import make_user
    from core.roles import Role

    admin = make_user(office, Role.ADMIN)
    apply_transition(awaiting, Action.RECORD_REPORT, officer)
    [note] = mine(reviewer)
    assert "report filed" in note.title and "ready for review" in note.title
    assert note.complaint == awaiting and note.read_at is None
    assert len(mine(admin)) == 1
    assert mine(clerk) == [] and mine(officer) == []  # not the clerk, not the sender


def test_the_person_who_acts_is_never_told_about_their_own_action(awaiting, reviewer):
    apply_transition(awaiting, Action.RECORD_REPORT, reviewer)
    assert mine(reviewer) == []


def test_asking_for_a_report_again_tells_the_officers(awaiting, clerk, reviewer, officer):
    apply_transition(awaiting, Action.RECORD_REPORT, clerk)
    Notification.objects.all().delete()
    apply_transition(awaiting, Action.REVIEW_NOT_SATISFIED, reviewer, note="Too vague")
    [note] = mine(officer)
    assert "please report again" in note.title and "Too vague" in note.body
    assert note.tone == "warn"


def test_a_portal_sending_it_back_tells_the_team(make_complaint, clerk, reviewer, tehsildar):
    c = make_complaint(source_platform=Platform.SAMADHAN_SHIVIR)
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    apply_transition(c, Action.RECORD_REPORT, clerk)
    apply_transition(c, Action.REVIEW_SATISFIED, reviewer)
    apply_transition(c, Action.SUBMIT_TO_PORTAL, clerk)
    Notification.objects.all().delete()
    apply_transition(
        c, Action.PORTAL_REJECTED, clerk, reason="Not satisfied", next_step="review_again"
    )
    [note] = mine(reviewer)
    assert note.title == "Complaint re-opened on Samadhan Shivir portal"
    assert "Not satisfied" in note.body and note.tone == "bad"
    assert mine(clerk) == []  # the clerk recorded it


def test_old_history_is_not_news(make_complaint, clerk, tehsildar, officer):
    c = make_complaint()
    long_ago = timezone.now() - timedelta(days=30)
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar, at=long_ago)
    assert Notification.objects.count() == 0


def test_custom_stages_and_notes_notify_nobody(make_complaint, clerk):
    from complaints.workflow import add_note, add_stage

    c = make_complaint()
    add_note(c, clerk, "called")
    add_stage(c, clerk, "Site visit")
    assert Notification.objects.count() == 0


# ----------------------------------------------------------- bell, page, reading


def make_note(user, complaint, **kw):
    return Notification.objects.create(
        office=complaint.office, recipient=user, complaint=complaint,
        title=kw.pop("title", "BDPO report filed: ready for review"), body="C-1 - Drain", **kw,
    )  # fmt: skip


def test_the_bell_shows_the_unread_count_and_latest(client, reviewer, complaint):
    make_note(reviewer, complaint, title="First")
    make_note(reviewer, complaint, title="Second", read_at=timezone.now())
    client.force_login(reviewer)
    html = client.get(reverse("complaints:list")).content.decode()
    assert 'class="bell-count">1<' in html
    assert "First" in html and "Second" in html


def test_no_unread_means_no_badge(client, reviewer, complaint):
    make_note(reviewer, complaint, read_at=timezone.now())
    client.force_login(reviewer)
    assert "bell-count" not in client.get(reverse("complaints:list")).content.decode()


def test_the_greeting_appears_once_per_login(client, reviewer, complaint):
    make_note(reviewer, complaint, title="BDPO report filed: ready for review")
    client.force_login(reviewer)
    first = client.get(reverse("complaints:list")).content.decode()
    assert "You have 1 new notification." in first
    again = client.get(reverse("complaints:list")).content.decode()
    assert "You have 1 new notification" not in again
    assert "bell-count" in again  # the badge stays until it is read


def test_the_greeting_is_not_used_up_by_background_requests(client, reviewer, complaint):
    make_note(reviewer, complaint)
    client.force_login(reviewer)
    client.get(reverse("complaints:list"), **HX)
    client.get(reverse("complaints:notification_bell"))
    assert "You have 1 new notification" in client.get(reverse("complaints:list")).content.decode()


def test_opening_one_marks_it_read_and_goes_to_the_case(client, reviewer, complaint):
    note = make_note(reviewer, complaint)
    client.force_login(reviewer)
    response = client.get(reverse("complaints:notification_open", args=[note.pk]))
    assert response.status_code == 302
    assert response["Location"] == f"{reverse('complaints:list')}?case={complaint.pk}"
    note.refresh_from_db()
    assert note.read_at is not None


def test_the_list_opens_the_case_a_notification_points_to(client, reviewer, complaint):
    client.force_login(reviewer)
    html = client.get(reverse("complaints:list"), {"case": complaint.pk}).content.decode()
    assert f'data-open="/c/{complaint.pk}/"' in html
    assert (
        "data-open"
        not in client.get(reverse("complaints:list"), {"case": "99999"}).content.decode()
    )


def test_you_cannot_open_or_read_someone_elses_notification(client, reviewer, clerk, complaint):
    note = make_note(reviewer, complaint)
    client.force_login(clerk)
    assert client.get(reverse("complaints:notification_open", args=[note.pk])).status_code == 404
    client.post(reverse("complaints:notifications_read_all"))
    note.refresh_from_db()
    assert note.read_at is None


def test_mark_all_read(client, reviewer, complaint):
    make_note(reviewer, complaint)
    make_note(reviewer, complaint)
    client.force_login(reviewer)
    response = client.post(reverse("complaints:notifications_read_all"), **HX)
    assert response.status_code == 200 and "bell-count" not in response.content.decode()
    assert service.unread_count(reviewer) == 0


def test_the_page_lists_filters_and_shows_what_needs_attention(
    client, reviewer, complaint, clerk, tehsildar
):
    make_note(reviewer, complaint, title="Old news", read_at=timezone.now())
    make_note(reviewer, complaint, title="Fresh news")
    apply_transition(complaint, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    apply_transition(complaint, Action.RECORD_REPORT, clerk)  # now waiting for review
    client.force_login(reviewer)
    html = client.get(reverse("complaints:notifications")).content.decode()
    assert "Old news" in html and "Fresh news" in html
    assert "1 report is waiting for your review" in html
    page = client.get(reverse("complaints:notifications"), {"show": "unread"}).content.decode()
    unread = page.split('class="notif-list"')[1]  # the page's own list, not the bell's dropdown
    assert "Fresh news" in unread and "Old news" not in unread


def test_overdue_reports_are_flagged_live(client, reviewer, awaiting):
    awaiting.referrals.update(due_on=timezone.localdate() - timedelta(days=2))
    client.force_login(reviewer)
    html = client.get(reverse("complaints:notifications")).content.decode()
    assert "1 report is overdue" in html


def test_an_officer_sees_what_is_waiting_for_their_report(client, officer, awaiting):
    client.force_login(officer)
    html = client.get(reverse("complaints:notifications")).content.decode()
    assert "1 complaint is waiting for your report" in html


def test_end_to_end_the_officer_uploads_and_the_reviewer_is_told(
    client, officer, reviewer, awaiting
):
    client.force_login(officer)
    upload = SimpleUploadedFile("report.pdf", b"%PDF-1.4 report", "application/pdf")
    client.post(
        reverse("complaints:action", args=[awaiting.pk, "record_report"]), {"file": upload}, **HX
    )
    client.force_login(reviewer)
    html = client.get(reverse("complaints:list")).content.decode()
    assert "You have 1 new notification." in html and "ready for review" in html
    note = Notification.objects.get(recipient=reviewer)
    follow = client.get(reverse("complaints:notification_open", args=[note.pk]), follow=False)
    assert follow["Location"].endswith(f"case={awaiting.pk}")
