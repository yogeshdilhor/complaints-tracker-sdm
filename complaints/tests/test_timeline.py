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
