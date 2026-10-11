"""Officer logins: made on the spot, valid for a month, limited to reporting."""

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from complaints.models import Document, Platform
from complaints.services import logins
from complaints.services.timeline import build_timeline
from complaints.workflow import Action, add_note, apply_transition, attach_document
from core.models import User
from core.roles import Role

HX = {"HTTP_HX_REQUEST": "true"}


def mark(client, user, complaint, action="issue_notice", **data):
    client.force_login(user)
    return client.post(reverse("complaints:action", args=[complaint.pk, action]), data, **HX)


# ------------------------------------------------------------ made when marking


def test_marking_with_create_login_makes_the_login_and_shows_it_once(
    client, reviewer, make_complaint, bdpo
):
    c = make_complaint()
    response = mark(
        client, reviewer, c, department=bdpo.pk, create_login="on", officer_name="Shri R. Singh"
    )
    html = response.content.decode()
    user = User.objects.get(department=bdpo)
    assert user.username == "bdpo" and user.first_name == "Shri R. Singh"
    assert user.has_role(Role.DEPARTMENT_OFFICER) and not user.is_staff
    assert user.login_expires_on == timezone.localdate() + timedelta(days=30)
    assert "Login created for BDPO" in html and "bdpo" in html
    password = html.split('class="cred-password">')[1].split("<")[0]
    assert len(password) == logins.PASSWORD_LENGTH
    assert client.login(username="bdpo", password=password)
    # shown only once
    again = client.get(reverse("complaints:detail", args=[c.pk])).content.decode()
    assert "Login created for" not in again


def test_without_the_box_no_login_is_made(client, reviewer, make_complaint, bdpo):
    mark(client, reviewer, make_complaint(), department=bdpo.pk)
    assert not User.objects.filter(department=bdpo).exists()


def test_writing_my_own_officer_and_creating_the_login_together(client, reviewer, make_complaint):
    c = make_complaint()
    mark(client, reviewer, c, department="", department_own="EXIN Irrigation", create_login="on")
    user = User.objects.get(username="exin-irrigation")
    assert user.department.name == "EXIN Irrigation"
    assert user.department == c.__class__.objects.get(pk=c.pk).current_department


def test_a_clerk_cannot_create_logins(client, clerk, make_complaint, bdpo):
    html = client.get  # noqa: F841
    client.force_login(clerk)
    page = client.get(
        reverse("complaints:action", args=[make_complaint().pk, "issue_notice"]), **HX
    ).content.decode()
    assert "Also create a login" not in page
    mark(client, clerk, make_complaint(), department=bdpo.pk, create_login="on")
    assert not User.objects.filter(department=bdpo).exists()


def test_a_second_login_for_the_same_office_gets_a_numbered_name(
    client, reviewer, make_complaint, bdpo
):
    for _ in range(2):
        c = make_complaint()
        mark(client, reviewer, c, department=bdpo.pk, create_login="on")
    assert set(User.objects.filter(department=bdpo).values_list("username", flat=True)) == {
        "bdpo",
        "bdpo2",
    }


def test_the_optional_limit_stops_new_logins_and_undoes_the_marking(
    client, reviewer, make_complaint, bdpo, tehsildar, settings
):
    settings.MAX_OFFICER_LOGINS = 1
    mark(client, reviewer, make_complaint(), department=bdpo.pk, create_login="on")
    c = make_complaint()
    response = mark(client, reviewer, c, department=tehsildar.pk, create_login="on")
    assert "limit of 1" in response.content.decode()
    c.refresh_from_db()
    assert c.current_state == "RECEIVED"  # the marking was undone, not half-done
    assert User.objects.filter(groups__name=Role.DEPARTMENT_OFFICER).count() == 1


def test_there_is_no_limit_unless_one_is_set(client, reviewer, make_complaint, office, settings):
    assert settings.MAX_OFFICER_LOGINS == 0
    from core.models import Department

    for i in range(12):
        d = Department.objects.create(
            office=office, name=f"Office {i}", short_name=f"O{i}", kind="external_department"
        )
        logins.create_officer_login(d)
    assert logins.active_logins(office).count() == 12


def test_a_login_cannot_be_made_when_the_case_is_not_marked_to_anyone(
    client, reviewer, make_complaint, clerk, tehsildar
):
    c = make_complaint(source_platform=Platform.CM_WINDOW)
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    apply_transition(c, Action.RECORD_REPORT, clerk)
    apply_transition(c, Action.REVIEW_SATISFIED, reviewer)
    apply_transition(c, Action.SUBMIT_TO_PORTAL, clerk)
    response = mark(
        client, reviewer, c, "portal_rejected",
        reason="No", next_step="resubmit", create_login="on",
    )  # fmt: skip
    assert "only be made when the case is marked to an officer" in response.content.decode()
    c.refresh_from_db()
    assert c.current_state == "PORTAL_SUBMITTED"


# -------------------------------------------------------------------- expiry


def test_an_expired_login_cannot_sign_in(client, office, bdpo):
    creds = logins.create_officer_login(bdpo)
    assert client.login(username=creds.username, password=creds.password)
    User.objects.filter(username=creds.username).update(
        login_expires_on=timezone.localdate() - timedelta(days=1)
    )
    client.logout()
    assert not client.login(username=creds.username, password=creds.password)


def test_the_last_day_still_works(client, office, bdpo):
    creds = logins.create_officer_login(bdpo)
    User.objects.filter(username=creds.username).update(login_expires_on=timezone.localdate())
    assert client.login(username=creds.username, password=creds.password)


def test_someone_already_signed_in_is_signed_out_when_it_expires(client, office, bdpo):
    creds = logins.create_officer_login(bdpo)
    client.login(username=creds.username, password=creds.password)
    assert client.get(reverse("complaints:list")).status_code == 200
    User.objects.filter(username=creds.username).update(
        login_expires_on=timezone.localdate() - timedelta(days=1)
    )
    response = client.get(reverse("complaints:list"))
    assert response.status_code == 302 and "/login/" in response["Location"]


def test_staff_accounts_never_expire(clerk, client):
    assert clerk.login_expires_on is None and not clerk.is_login_expired


# ------------------------------------------------- what the officer can see and do


@pytest.fixture
def story(make_complaint, clerk, reviewer, bdpo, tehsildar, make_document):
    """One case marked to the BDPO, then also to the Tehsildar, with a note and a portal step."""
    c = make_complaint(source_platform=Platform.SAMADHAN_SHIVIR)
    letter = make_document(c, Document.Kind.COMPLAINT, "letter")
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=bdpo)
    bdpo_report = make_document(c, Document.Kind.INQUIRY_REPORT, "bdpo-report")
    apply_transition(c, Action.RECORD_REPORT, clerk, document=bdpo_report)
    apply_transition(
        c, Action.REVIEW_NOT_SATISFIED, reviewer, note="Too vague", department=tehsildar
    )
    tehsildar_report = make_document(c, Document.Kind.INQUIRY_REPORT, "tehsildar-report")
    apply_transition(c, Action.RECORD_REPORT, clerk, document=tehsildar_report)
    add_note(c, clerk, "Secret SDM thought")
    secret = attach_document(c, clerk, _att("internal-memo"), Document.Kind.OTHER)
    return c, letter, bdpo_report, tehsildar_report, secret


def _att(label):
    from complaints.tests.helpers import fake_attachment

    return fake_attachment(label)


def officer_for(department, office):
    from complaints.tests.conftest import make_user

    return make_user(office, Role.DEPARTMENT_OFFICER, department=department)


def test_an_officer_sees_only_their_own_stages(client, office, bdpo, story):
    c, *_ = story
    client.force_login(officer_for(bdpo, office))
    titles = [s.title for s in build_timeline(c, department=bdpo)]
    assert titles == [
        "Complaint received",
        "Marked to BDPO for report",
        "BDPO report filed",
        "Report found inadequate; Tehsildar asked to report again",
    ] or titles[:3] == ["Complaint received", "Marked to BDPO for report", "BDPO report filed"]
    html = client.get(reverse("complaints:detail", args=[c.pk])).content.decode()
    assert "BDPO report filed" in html
    assert "Secret SDM thought" not in html and "Tehsildar report filed" not in html
    assert "internal-memo" not in html and "tehsildar-report" not in html


def test_an_officer_can_open_only_the_documents_on_their_stages(client, office, bdpo, story):
    c, letter, bdpo_report, tehsildar_report, secret = story
    client.force_login(officer_for(bdpo, office))

    def status(doc):
        return client.get(reverse("complaints:document", args=[doc.pk])).status_code

    assert status(letter) == 200 and status(bdpo_report) == 200
    assert status(tehsildar_report) == 404 and status(secret) == 404


def test_staff_still_see_everything(client, reviewer, story):
    c, *_ = story
    client.force_login(reviewer)
    html = client.get(reverse("complaints:detail", args=[c.pk])).content.decode()
    assert "Secret SDM thought" in html and "tehsildar-report" in html


def test_an_officer_sees_all_cases_marked_to_them_over_time(
    client, office, bdpo, story, make_complaint, clerk
):
    c, *_ = story
    other = make_complaint()
    apply_transition(other, Action.ISSUE_NOTICE, clerk, department=bdpo)
    unrelated = make_complaint()
    client.force_login(officer_for(bdpo, office))
    html = client.get(reverse("complaints:list")).content.decode()
    assert c.ref_no in html and other.ref_no in html and unrelated.ref_no not in html


def test_an_officer_cannot_summarise_export_or_manage(client, office, bdpo, story):
    c, letter, *_ = story
    client.force_login(officer_for(bdpo, office))
    assert client.post(reverse("complaints:document_summary", args=[letter.pk])).status_code == 403
    for name in ("complaints:dashboard", "complaints:export", "complaints:officer_logins"):
        assert client.get(reverse(name)).status_code == 403
    nav = (
        client.get(reverse("complaints:list")).content.decode().split("<nav>")[1].split("</nav>")[0]
    )
    assert "Officer logins" not in nav and "Dashboard" not in nav


def test_an_officer_can_still_upload_their_report(client, office, make_complaint, clerk, bdpo):
    c = make_complaint()
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=bdpo)
    client.force_login(officer_for(bdpo, office))
    upload = SimpleUploadedFile("r.pdf", b"%PDF-1.4 mine", "application/pdf")
    response = client.post(
        reverse("complaints:action", args=[c.pk, "record_report"]), {"file": upload}, **HX
    )
    assert response.status_code == 200
    c.refresh_from_db()
    assert c.current_state == "UNDER_REVIEW"


# ------------------------------------------------------------ the logins page


def test_the_page_lists_logins_with_their_state(client, reviewer, office, bdpo, tehsildar):
    logins.create_officer_login(bdpo, "Shri A")
    b = logins.create_officer_login(tehsildar)
    User.objects.filter(username=b.username).update(
        login_expires_on=timezone.localdate() - timedelta(days=2)
    )
    client.force_login(reviewer)
    html = client.get(reverse("complaints:officer_logins")).content.decode()
    assert "Shri A" in html and "Active" in html and "Expired" in html
    assert "no limit" in html.lower()


def test_creating_a_login_from_the_page(client, reviewer, bdpo):
    client.force_login(reviewer)
    response = client.post(
        reverse("complaints:officer_login_new"),
        {"department": bdpo.pk, "name": "Shri B", "phone": "9000000001"},
        follow=True,
    )
    html = response.content.decode()
    assert "Login created for BDPO" in html
    assert User.objects.get(username="bdpo").phone == "9000000001"


def test_reset_extend_switch_off_and_on(client, reviewer, office, bdpo):
    creds = logins.create_officer_login(bdpo)
    user = User.objects.get(username=creds.username)
    client.force_login(reviewer)

    def act(action):
        return client.post(
            reverse("complaints:officer_login_action", args=[user.pk, action]), follow=True
        )

    html = act("reset").content.decode()
    assert "Login created for BDPO" in html
    user.refresh_from_db()
    assert not user.check_password(creds.password)

    user.login_expires_on = timezone.localdate() + timedelta(days=3)
    user.save()
    act("extend")
    user.refresh_from_db()
    assert user.login_expires_on == timezone.localdate() + timedelta(days=33)

    act("off")
    user.refresh_from_db()
    assert not user.is_active and not logins.active_logins(office).exists()
    act("on")
    user.refresh_from_db()
    assert user.is_active


def test_switching_one_on_respects_the_limit(client, reviewer, office, bdpo, tehsildar, settings):
    a = logins.create_officer_login(bdpo)
    b = User.objects.get(username=logins.create_officer_login(tehsildar).username)
    logins.switch_off(b)
    settings.MAX_OFFICER_LOGINS = 1
    client.force_login(reviewer)
    html = client.post(
        reverse("complaints:officer_login_action", args=[b.pk, "on"])
    ).content.decode()
    assert "limit of 1" in html
    b.refresh_from_db()
    assert not b.is_active and a.username


def test_only_the_sdm_side_can_manage_logins_and_only_officer_ones(
    client, clerk, reviewer, office, bdpo
):
    creds = logins.create_officer_login(bdpo)
    officer_user = User.objects.get(username=creds.username)
    client.force_login(clerk)
    assert client.get(reverse("complaints:officer_logins")).status_code == 403
    assert (
        client.post(
            reverse("complaints:officer_login_action", args=[officer_user.pk, "off"])
        ).status_code
        == 403
    )
    client.force_login(reviewer)
    # a staff account is not an officer login and cannot be reached this way
    assert (
        client.post(reverse("complaints:officer_login_action", args=[clerk.pk, "off"])).status_code
        == 404
    )
    clerk.refresh_from_db()
    assert clerk.is_active


def test_passwords_avoid_look_alike_characters():
    seen = "".join(logins._new_password() for _ in range(200))
    assert not set(seen) & set("0O1lI")
