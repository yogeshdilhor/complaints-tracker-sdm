"""The case timeline: stages in your own words, each carrying the documents of that step."""

from complaints.models import Document, Platform
from complaints.services.documents import store_document
from complaints.services.timeline import build_timeline
from complaints.tests.helpers import fake_attachment
from complaints.workflow import Action, apply_transition, attach_document


def titles(complaint):
    return [s.title for s in build_timeline(complaint)]


def test_the_journey_reads_in_your_words(make_complaint, clerk, reviewer, tehsildar, make_document):
    c = make_complaint(source_platform=Platform.SAMADHAN_SHIVIR)
    notice = make_document(c, Document.Kind.NOTICE)
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar, notice_document=notice)
    report = make_document(c, Document.Kind.INQUIRY_REPORT)
    apply_transition(c, Action.RECORD_REPORT, clerk, document=report)
    apply_transition(c, Action.REVIEW_SATISFIED, reviewer)
    atr = make_document(c, Document.Kind.ATR)
    apply_transition(c, Action.SUBMIT_TO_PORTAL, clerk, document=atr, portal_ack_no="SS-77")
    apply_transition(
        c, Action.PORTAL_REJECTED, clerk, reason="Not satisfied", next_step="review_again"
    )
    assert titles(c) == [
        "Complaint received",
        "Marked to Tehsildar for report",
        "Tehsildar report filed",
        "Report accepted by SDM",
        "ATR uploaded on Samadhan Shivir portal",
        "Complaint re-opened on Samadhan Shivir portal",
    ]
    stages = {s.title: s for s in build_timeline(c)}
    assert stages["Marked to Tehsildar for report"].documents == [notice]
    assert stages["Tehsildar report filed"].documents == [report]
    assert stages["ATR uploaded on Samadhan Shivir portal"].documents == [atr]
    assert "SS-77" in stages["ATR uploaded on Samadhan Shivir portal"].detail
    assert "Not satisfied" in stages["Complaint re-opened on Samadhan Shivir portal"].detail


def test_asking_for_a_second_report_and_closing(make_complaint, clerk, reviewer, tehsildar):
    c = make_complaint()
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    apply_transition(c, Action.RECORD_REPORT, clerk)
    apply_transition(c, Action.REVIEW_NOT_SATISFIED, reviewer, note="Too vague")
    apply_transition(c, Action.RECORD_REPORT, clerk)
    apply_transition(c, Action.REVIEW_SATISFIED, reviewer)
    apply_transition(c, Action.CLOSE_WITHOUT_PORTAL, reviewer, reason="Matter settled")
    assert titles(c)[3:] == [
        "Report found inadequate; Tehsildar asked to report again",
        "Tehsildar report filed",
        "Report accepted by SDM",
        "Resolved and filed",
    ]


def test_a_complaint_not_from_a_portal_says_just_portal(make_complaint, clerk, reviewer, tehsildar):
    c = make_complaint(source_platform=Platform.INTERNAL)
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    apply_transition(c, Action.RECORD_REPORT, clerk)
    apply_transition(c, Action.REVIEW_SATISFIED, reviewer)
    apply_transition(c, Action.SUBMIT_TO_PORTAL, clerk)
    assert titles(c)[-1] == "ATR uploaded on portal"


def test_added_documents_sit_on_their_own_stage(make_complaint, clerk):
    c = make_complaint()
    doc = attach_document(c, clerk, fake_attachment("extra"), Document.Kind.OTHER)
    last = build_timeline(c)[-1]
    assert last.title == "Document added (other)" and last.documents == [doc]


def test_a_document_no_event_points_at_is_not_lost(make_complaint, clerk):
    c = make_complaint()
    stray = store_document(c, fake_attachment("stray"), Document.Kind.OTHER, clerk)
    last = build_timeline(c)[-1]
    assert last.title == "Other documents" and last.documents == [stray]


def test_the_detail_page_shows_one_timeline_with_documents_inside(
    client, clerk, make_complaint, tehsildar, make_document
):
    from django.urls import reverse

    c = make_complaint()
    notice = make_document(c, Document.Kind.NOTICE, "bdpo-notice")
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar, notice_document=notice)
    client.force_login(clerk)
    html = client.get(reverse("complaints:detail", args=[c.pk])).content.decode()
    assert "Case timeline" in html and "Marked to Tehsildar for report" in html
    assert "bdpo-notice.pdf" in html
    assert "Case documents" not in html and ">History<" not in html


# ------------------------------------------------- headlines and typed-in officers


def test_a_chosen_headline_replaces_the_default_title(make_complaint, clerk, tehsildar):
    c = make_complaint()
    apply_transition(
        c, Action.ISSUE_NOTICE, clerk, department=tehsildar, headline="Sent to Tehsildar urgently"
    )
    stage = build_timeline(c)[-1]
    assert stage.title == "Sent to Tehsildar urgently"
    assert "Report due by" in stage.detail  # the rest of the stage is unchanged


def test_headline_is_trimmed_capped_and_optional(make_complaint, clerk, tehsildar):
    c = make_complaint()
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar, headline="  ")
    assert build_timeline(c)[-1].title == "Marked to Tehsildar for report"
    apply_transition(c, Action.RECORD_REPORT, clerk, headline="x" * 500)
    assert len(build_timeline(c)[-1].title) == 200


def test_notes_and_documents_can_carry_a_headline(make_complaint, clerk):
    from complaints.workflow import add_note

    c = make_complaint()
    add_note(c, clerk, "Called the BDPO", headline="Phone call with BDPO")
    attach_document(
        c, clerk, fake_attachment("x"), Document.Kind.OTHER, headline="Photo from the site"
    )
    assert titles(c)[-2:] == ["Phone call with BDPO", "Photo from the site"]


def _post(client, user, complaint, action, data):
    from django.urls import reverse

    client.force_login(user)
    return client.post(
        reverse("complaints:action", args=[complaint.pk, action]),
        data,
        HTTP_HX_REQUEST="true",
    )


def test_the_action_form_saves_the_picked_headline(client, clerk, make_complaint, tehsildar):
    c = make_complaint()
    _post(client, clerk, c, "issue_notice", {"department": tehsildar.pk, "headline": "My title"})
    assert titles(c)[-1] == "My title"


def test_writing_my_own_officer_creates_that_department(client, clerk, make_complaint, office):
    from core.models import Department

    c = make_complaint()
    _post(client, clerk, c, "issue_notice", {"department": "", "department_own": "EXIN DHBVN"})
    c.refresh_from_db()
    assert c.current_department.name == "EXIN DHBVN"
    assert c.current_department.kind == Department.Kind.EXTERNAL_DEPARTMENT
    # Typing the same name again reuses it instead of making a duplicate.
    d = make_complaint()
    _post(client, clerk, d, "issue_notice", {"department": "", "department_own": "exin dhbvn"})
    assert Department.objects.filter(office=office, name__iexact="EXIN DHBVN").count() == 1


def test_an_officer_is_still_required(client, clerk, make_complaint):
    c = make_complaint()
    response = _post(client, clerk, c, "issue_notice", {"department": ""})
    assert "Choose who this goes to" in response.content.decode()
    c.refresh_from_db()
    assert c.current_state == "RECEIVED"


def test_the_form_renders_wheels_for_the_officer_and_the_title(
    client, clerk, make_complaint, tehsildar
):
    from django.urls import reverse

    c = make_complaint()
    client.force_login(clerk)
    html = client.get(
        reverse("complaints:action", args=[c.pk, "issue_notice"]), HTTP_HX_REQUEST="true"
    ).content.decode()
    assert html.count('class="wheel-field"') == 2
    assert "Marked to {officer} for report" in html and "Write my own..." in html
    assert ">Tehsildar</li>" in html
