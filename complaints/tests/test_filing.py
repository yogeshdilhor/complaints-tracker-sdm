"""Filing a complaint: admin only, hidden everywhere but the Archive, and restorable."""

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from complaints import permissions as perms
from complaints.models import EventType, Platform
from complaints.services.analytics import build_dashboard
from complaints.services.timeline import build_timeline
from complaints.tests.conftest import make_user
from complaints.workflow import (
    Action,
    InvalidTransition,
    InvalidTransitionData,
    apply_transition,
    file_complaint,
    unfile_complaint,
)
from core.models import User
from core.roles import Role

HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def sdm(office):
    return make_user(office, Role.ADMIN)


def do(client, user, complaint, action, **data):
    client.force_login(user)
    return client.post(reverse("complaints:action", args=[complaint.pk, action]), data, **HX)


# --------------------------------------------------------------- who may file


def test_only_the_admin_may_file_or_restore(sdm, reviewer, clerk, auditor, officer, complaint):
    assert perms.can(sdm, perms.FILE, complaint)
    for other in (reviewer, clerk, auditor, officer):
        assert not perms.can(other, perms.FILE, complaint), other
    complaint = file_complaint(complaint, sdm, "x") and complaint
    assert perms.can(sdm, perms.UNFILE, complaint)
    for other in (reviewer, clerk, auditor, officer):
        assert not perms.can(other, perms.UNFILE, complaint), other


def test_the_reviewer_has_every_other_ability(reviewer, sdm, complaint):
    """The complaints branch (Reviewer) can do all the admin can, except file and restore."""
    from complaints.permissions import _ADMIN_ACTIONS, _REVIEWER_ACTIONS

    assert _ADMIN_ACTIONS - _REVIEWER_ACTIONS == {perms.FILE, perms.UNFILE}
    assert _REVIEWER_ACTIONS - _ADMIN_ACTIONS == set()


def test_filing_through_the_ui_needs_a_reason_and_the_admin(client, sdm, reviewer, complaint):
    assert do(client, reviewer, complaint, "file_complaint", reason="x").status_code == 403
    refused = do(client, sdm, complaint, "file_complaint", reason="")
    assert "required" in refused.content.decode().lower()
    complaint.refresh_from_db()
    assert not complaint.is_filed

    ok = do(client, sdm, complaint, "file_complaint", reason="Anonymous and untraceable")
    assert ok.status_code == 200 and "Complaint filed" in ok.content.decode()
    assert ok["HX-Trigger"] == "complaintChanged"
    complaint.refresh_from_db()
    assert complaint.is_filed and complaint.filed_by == sdm
    assert complaint.filing_reason == "Anonymous and untraceable"


def test_the_menu_offers_filing_to_the_admin_only(client, sdm, reviewer, complaint):
    url = reverse("complaints:action", args=[complaint.pk, "add_stage"])
    client.force_login(sdm)
    assert (
        "File this complaint (move to Archive)"
        in client.get(url, HTTP_HX_REQUEST="true").content.decode()
    )
    client.force_login(reviewer)
    assert "File this complaint" not in client.get(url, HTTP_HX_REQUEST="true").content.decode()


# ------------------------------------------------------------ where it vanishes


@pytest.fixture
def filed(make_complaint, sdm):
    c = make_complaint(subject="Filed away")
    file_complaint(c, sdm, "Resolved")
    return c


def test_a_filed_complaint_is_out_of_every_normal_view(
    client, reviewer, clerk, officer, tehsildar, filed, make_complaint
):
    live = make_complaint(subject="Still live")
    client.force_login(reviewer)
    assert filed.ref_no not in client.get(reverse("complaints:list")).content.decode()
    assert live.ref_no in client.get(reverse("complaints:list")).content.decode()
    assert list(perms.visible_complaints(reviewer)) == [live]
    assert build_dashboard(perms.visible_complaints(reviewer), "all")["total"] == 1
    assert filed.ref_no not in client.get(reverse("complaints:overdue")).content.decode()
    # officers never see it, even if it was marked to them
    other = make_complaint()
    apply_transition(other, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    file_complaint(other, User.objects.get(pk=filed.filed_by_id), "closed")
    assert other not in perms.visible_complaints(officer)


def test_a_filed_complaint_is_read_only(client, sdm, reviewer, filed):
    for action in ("add_stage", "issue_notice", "edit_details", "add_note"):
        assert do(client, reviewer, filed, action, headline="x").status_code == 403
        assert do(client, sdm, filed, action, headline="x").status_code == 403
    html = client.get(reverse("complaints:detail", args=[filed.pk])).content.decode()
    assert "Filed " in html and "Resolved" in html
    assert 'class="add-menu"' not in html.split("<main")[1]  # no way to add anything


def test_staff_can_still_open_a_filed_case_and_its_documents(
    client, reviewer, filed, make_document
):
    document = make_document(filed)
    client.force_login(reviewer)
    assert client.get(reverse("complaints:detail", args=[filed.pk])).status_code == 200
    assert client.get(reverse("complaints:document", args=[document.pk])).status_code == 200


def test_officers_get_nothing_for_a_filed_case(
    client, office, tehsildar, clerk, make_complaint, sdm
):
    from complaints.tests.conftest import make_user as mk

    c = make_complaint()
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    officer = mk(office, Role.DEPARTMENT_OFFICER, department=tehsildar)
    file_complaint(c, sdm, "x")
    client.force_login(officer)
    assert client.get(reverse("complaints:detail", args=[c.pk])).status_code == 404


# ------------------------------------------------------------------ the Archive


def test_the_archive_lists_filed_complaints_with_search(
    client, reviewer, filed, make_complaint, sdm
):
    other = make_complaint(subject="Second filed")
    file_complaint(other, sdm, "Duplicate of another")
    live = make_complaint(subject="Still live")
    client.force_login(reviewer)
    html = client.get(reverse("complaints:archive")).content.decode()
    assert filed.ref_no in html and other.ref_no in html and live.ref_no not in html
    assert "Resolved" in html and "Duplicate of another" in html
    found = client.get(reverse("complaints:archive"), {"q": "duplicate"}).content.decode()
    assert (
        other.ref_no in found.split("<tbody>")[1] and filed.ref_no not in found.split("<tbody>")[1]
    )
    assert "Nothing has been filed" not in html


def test_the_archive_is_for_staff_not_officers(client, officer, filed):
    client.force_login(officer)
    assert client.get(reverse("complaints:archive")).status_code == 403


def test_only_the_admin_can_restore(client, sdm, reviewer, filed):
    assert do(client, reviewer, filed, "unfile_complaint").status_code == 403
    page = client.get(reverse("complaints:detail", args=[filed.pk])).content.decode()
    assert "Restore" not in page
    client.force_login(sdm)
    assert "Restore" in client.get(reverse("complaints:detail", args=[filed.pk])).content.decode()
    response = do(client, sdm, filed, "unfile_complaint", reason="Filed by mistake")
    assert response.status_code == 200
    filed.refresh_from_db()
    assert not filed.is_filed
    assert filed in perms.visible_complaints(sdm)


def test_filing_and_restoring_leave_a_trail(sdm, complaint):
    file_complaint(complaint, sdm, "Resolved")
    unfile_complaint(complaint, sdm, "Mistake")
    titles = [s.title for s in build_timeline(complaint)]
    assert titles[-2:] == ["Complaint filed (moved to the Archive)", "Restored from the Archive"]
    kinds = list(complaint.events.values_list("type", flat=True))
    assert kinds[-2:] == [EventType.FILED, EventType.UNFILED]


def test_filing_twice_or_restoring_an_unfiled_one_is_refused(sdm, complaint):
    with pytest.raises(InvalidTransitionData):
        file_complaint(complaint, sdm, "  ")
    file_complaint(complaint, sdm, "x")
    with pytest.raises(InvalidTransition):
        file_complaint(complaint, sdm, "again")
    unfile_complaint(complaint, sdm)
    with pytest.raises(InvalidTransition):
        unfile_complaint(complaint, sdm)


def test_export_can_pick_active_filed_or_both(client, reviewer, filed, make_complaint):
    from complaints.services.export import export_queryset

    live = make_complaint()
    everything = perms.visible_complaints(reviewer, include_filed=True)
    assert set(export_queryset(everything)) == {live}
    assert set(export_queryset(everything, archive="only")) == {filed}
    assert set(export_queryset(everything, archive="both")) == {live, filed}


def test_the_dashboard_and_archive_links_are_in_the_nav(client, reviewer):
    client.force_login(reviewer)
    nav = (
        client.get(reverse("complaints:list")).content.decode().split("<nav>")[1].split("</nav>")[0]
    )
    assert "Archive" in nav


# ------------------------------------------------ the SDM and complaints-branch logins


def make(*args):
    out = io.StringIO()
    call_command("create_login", *args, stdout=out)
    return out.getvalue()


def test_create_login_makes_the_sdm_and_the_complaints_branch(office, client):
    sdm_out = make("--username", "sdm", "--role", "Admin", "--name", "SDM", "--admin-site")
    cb_out = make(
        "--username", "complaintsbranch", "--role", "Reviewer", "--name", "Complaints Branch"
    )
    sdm_password = sdm_out.split("Password: ")[1].split("\n")[0]
    cb_password = cb_out.split("Password: ")[1].split("\n")[0]
    sdm_user = User.objects.get(username="sdm")
    cb_user = User.objects.get(username="complaintsbranch")
    assert sdm_user.has_role(Role.ADMIN) and sdm_user.is_staff and sdm_user.login_expires_on is None
    assert cb_user.has_role(Role.REVIEWER) and not cb_user.is_staff
    assert client.login(username="sdm", password=sdm_password)
    assert client.login(username="complaintsbranch", password=cb_password)


def test_create_login_refuses_to_overwrite_unless_reset(office):
    make("--username", "sdm", "--role", "Admin")
    with pytest.raises(CommandError):
        make("--username", "sdm", "--role", "Admin")
    again = make("--username", "sdm", "--role", "Admin", "--reset")
    assert "Password:" in again


def test_password_change_page(client, sdm):
    client.force_login(sdm)
    assert client.get(reverse("password_change")).status_code == 200
    from django.contrib.auth import get_user_model

    sdm.set_password("Old-pass-12345")
    sdm.save()
    client.force_login(sdm)
    response = client.post(
        reverse("password_change"),
        {
            "old_password": "Old-pass-12345",
            "new_password1": "A-new-pass-9876",
            "new_password2": "A-new-pass-9876",
        },
    )
    assert response.status_code == 302
    assert get_user_model().objects.get(pk=sdm.pk).check_password("A-new-pass-9876")
    assert Platform.CM_WINDOW  # keeps the import used


# ------------------------------------------------------------------ the account menu


def test_the_account_menu_holds_change_password_and_log_out(client, sdm):
    client.force_login(sdm)
    html = client.get(reverse("complaints:list")).content.decode()
    menu = html.split('class="user-menu"')[1].split("</details>")[0]
    assert "Change password" in menu and "Log out" in menu
    assert reverse("password_change") in menu and reverse("logout") in menu
    assert "Admin (SDM)" in menu and f"Signed in as {sdm.username}" in menu
    # and they are no longer loose in the top bar
    bar = html.split('<header class="topbar">')[1].split("</header>")[0]
    assert bar.count("Log out") == 1 and bar.count("Change password") == 1


def test_the_menu_names_the_role(client, reviewer, officer, tehsildar):
    client.force_login(reviewer)
    assert "Complaints branch" in client.get(reverse("complaints:list")).content.decode()
    client.force_login(officer)
    html = client.get(reverse("complaints:list")).content.decode()
    assert "Department officer" in html and "Tehsildar" in html


def test_logging_out_from_the_menu_works(client, sdm):
    client.force_login(sdm)
    response = client.post(reverse("logout"))
    assert response.status_code == 302
    assert client.get(reverse("complaints:list")).status_code == 302


def test_the_stylesheet_has_balanced_braces():
    """One stray brace silently swallows every rule after it, so check it."""
    from pathlib import Path

    css = Path("static/css/app.css").read_text()
    assert css.count("{") == css.count("}")
