"""One timeline of a complaint's journey: each stage is an event plus the documents it carries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from complaints.models import (
    Complaint,
    Document,
    Event,
    EventType,
    InquiryReport,
    Platform,
    Referral,
)

# Stages that only carry words and documents; the only ones that may be deleted.
DELETABLE_STAGE_TYPES = (EventType.NOTE_ADDED, EventType.DOCUMENT_ATTACHED)

# Payload keys that point at a document, per event type.
_DOCUMENT_KEYS = ("document_id", "notice_document_id")


@dataclass
class Stage:
    title: str
    at: datetime
    actor: Any = None
    tone: str = "info"  # info, ok, warn, bad: the colour of the dot
    detail: str = ""
    documents: list[Document] = field(default_factory=list)
    event_id: int | None = None
    deletable: bool = False  # may the SDM take it off the timeline?

    @property
    def has_body(self) -> bool:
        return bool(self.detail or self.documents)


def portal_name(complaint: Complaint) -> str:
    """'Samadhan Shivir portal'; a plain 'portal' for complaints that did not come from one."""
    if complaint.source_platform == Platform.INTERNAL:
        return "portal"
    return f"{complaint.get_source_platform_display()} portal"


def event_document_ids(event: Event) -> list[int]:
    p = event.payload
    ids = [p.get(key) for key in _DOCUMENT_KEYS]
    ids.append((p.get("new_referral") or {}).get("notice_document_id"))
    ids.extend(p.get("document_ids") or [])
    return [i for i in dict.fromkeys(ids) if i]


def describe_event(event: Event, complaint: Complaint) -> tuple[str, str, str]:
    """(title, tone, detail) for one event."""
    p = event.payload
    title, tone, detail = _default_description(event, complaint)
    return (p.get("headline") or title), tone, detail


def _default_description(event: Event, complaint: Complaint) -> tuple[str, str, str]:
    p = event.payload
    portal = portal_name(complaint)
    match event.type:
        case EventType.COMPLAINT_REGISTERED:
            via = complaint.get_source_platform_display()
            related = f"; related to {p['related_to']}" if p.get("related_to") else ""
            return "Complaint received", "info", f"Received through {via}{related}"
        case EventType.NOTICE_ISSUED:
            dept = p.get("department", "")
            if p.get("round_no", 1) > 1:
                title = f"Marked to {dept} again for report (round {p['round_no']})"
            else:
                title = f"Marked to {dept} for report"
            return title, "info", f"Report due by {p.get('due_on', '')}"
        case EventType.REPORT_RECEIVED:
            return f"{p.get('department', 'Department')} report filed", "ok", ""
        case EventType.REVIEW_SATISFIED:
            return "Report accepted by SDM", "ok", p.get("note", "")
        case EventType.REVIEW_NOT_SATISFIED:
            dept = (p.get("new_referral") or {}).get("department", "the department")
            return (
                f"Report found inadequate; {dept} asked to report again",
                "warn",
                p.get("note", ""),
            )
        case EventType.PORTAL_SUBMITTED:
            ack = f"Acknowledgement no. {p['portal_ack_no']}" if p.get("portal_ack_no") else ""
            return f"ATR uploaded on {portal}", "info", ack
        case EventType.PORTAL_ACCEPTED:
            ack = f"Acknowledgement no. {p['portal_ack_no']}" if p.get("portal_ack_no") else ""
            return f"Complaint closed on {portal}", "ok", ack
        case EventType.PORTAL_REJECTED:
            step = (p.get("next_step") or "").replace("_", " ")
            detail = p.get("reason", "") + (f" (next: {step})" if step else "")
            return f"Complaint re-opened on {portal}", "bad", detail
        case EventType.CLOSED:
            return "Resolved and filed", "ok", p.get("reason", "")
        case EventType.REOPENED:
            return "Complaint re-opened", "bad", p.get("reason", "")
        case EventType.FILED:
            return "Complaint filed (moved to the Archive)", "info", p.get("reason", "")
        case EventType.UNFILED:
            return "Restored from the Archive", "info", p.get("reason", "")
        case EventType.DOCUMENT_ATTACHED:
            kind = (p.get("kind") or "document").replace("_", " ")
            return f"Document added ({kind})", "info", ""
        case EventType.NOTE_ADDED:
            return "Note added", "info", p.get("note", "")
        case EventType.STAGE_DELETED:
            said = f"Reason: {p.get('reason', '')}"
            return f"Stage deleted: {p.get('title', '')}", "bad", said
        case EventType.DETAILS_EDITED:
            changes = "; ".join(
                f"{name}: {v['old'] or '-'} -> {v['new'] or '-'}"
                for name, v in p.get("changes", {}).items()
            )
            return "Details corrected", "info", changes
    return event.get_type_display(), "info", ""


def _stages_for_department(complaint: Complaint, department) -> set[int]:
    """Ids of the events that concern `department`: its notices, its reports, and the verdicts
    on them. Everything else (other departments, notes, portals) is none of its business."""
    referrals = set(
        Referral.objects.filter(complaint=complaint, to_department=department).values_list(
            "pk", flat=True
        )
    )
    reports = set(
        InquiryReport.objects.filter(referral_id__in=referrals).values_list("pk", flat=True)
    )
    allowed: set[int] = set()
    for event in complaint.events.select_related("actor"):
        p = event.payload
        match event.type:
            case EventType.COMPLAINT_REGISTERED:
                allowed.add(event.pk)
            case EventType.NOTICE_ISSUED | EventType.REPORT_RECEIVED:
                if p.get("referral_id") in referrals:
                    allowed.add(event.pk)
            case EventType.DOCUMENT_ATTACHED:
                if event.actor.department_id == department.pk:  # the office's own response
                    allowed.add(event.pk)
            case EventType.REVIEW_SATISFIED:
                if p.get("report_id") in reports:
                    allowed.add(event.pk)
            case EventType.REVIEW_NOT_SATISFIED:
                if (
                    p.get("report_id") in reports
                    or (p.get("new_referral") or {}).get("referral_id") in referrals
                ):
                    allowed.add(event.pk)
    return allowed


def build_timeline(complaint: Complaint, department=None) -> list[Stage]:
    """Oldest first. Documents no event points at go in a closing "Other documents" stage.

    With `department`, only the stages that concern that department (for an officer's login).
    """
    visible_events = _stages_for_department(complaint, department) if department else None
    events = list(complaint.events.select_related("actor"))
    deleted = {e.payload.get("event_id") for e in events if e.type == EventType.STAGE_DELETED}
    documents = {
        d.pk: d
        for d in complaint.documents.filter(is_deleted=False)
        .select_related("supersedes")
        .prefetch_related("superseded_by")
    }
    stages: list[Stage] = []
    for event in events:
        if event.pk in deleted:
            continue
        if visible_events is not None and event.pk not in visible_events:
            continue
        title, tone, detail = describe_event(event, complaint)
        attached = [documents.pop(i) for i in event_document_ids(event) if i in documents]
        stages.append(
            Stage(
                title,
                event.occurred_at,
                event.actor,
                tone,
                detail,
                attached,
                event_id=event.pk,
                deletable=event.type in DELETABLE_STAGE_TYPES,
            )
        )
    if department is not None and stages:
        # The complaint's own paper belongs to the first stage, linked by an event or not.
        stages[0].documents += [d for d in documents.values() if d.kind == Document.Kind.COMPLAINT]
    if documents and department is None:
        leftover = sorted(documents.values(), key=lambda d: d.uploaded_at)
        stages.append(
            Stage("Other documents", leftover[-1].uploaded_at, None, "info", "", leftover)
        )
    return stages
