"""Who hears about what: a notification for each person an event matters to, plus the live
"needs attention" counts that are worked out when the page is opened."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import QuerySet
from django.urls import reverse
from django.utils import timezone

from complaints.models import Complaint, Event, EventType, Notification, Referral, State
from complaints.services.timeline import describe_event
from core.models import User
from core.roles import Role

# Back-dated events (imported or seeded history) are not news.
RECENT = timedelta(days=2)
SDM_SIDE = (Role.REVIEWER, Role.ADMIN)
SDM_TEAM = (Role.CLERK, Role.REVIEWER, Role.ADMIN)


def _staff(complaint: Complaint, roles) -> QuerySet[User]:
    return User.objects.filter(
        office_id=complaint.office_id, is_active=True, groups__name__in=roles
    ).distinct()


def _officers(complaint: Complaint, department_id: int | None) -> QuerySet[User]:
    if not department_id:
        return User.objects.none()
    return User.objects.filter(
        office_id=complaint.office_id,
        is_active=True,
        groups__name=Role.DEPARTMENT_OFFICER,
        department_id=department_id,
    ).distinct()


def notify_event(event: Event, complaint: Complaint) -> None:
    """Create the notifications this event deserves (never for the person who caused it)."""
    if timezone.now() - event.occurred_at > RECENT:
        return
    p = event.payload
    title, tone, _ = describe_event(event, complaint)
    case = f"{complaint.ref_no} - {complaint.subject}"
    plans: list[tuple[QuerySet[User], str, str, str]] = []

    match event.type:
        case EventType.REPORT_RECEIVED:
            plans.append((_staff(complaint, SDM_SIDE), f"{title}: ready for review", case, tone))
        case EventType.NOTICE_ISSUED:
            due = p.get("due_on", "")
            plans.append(
                (
                    _officers(complaint, p.get("department_id")),
                    "A complaint has been marked to you",
                    f"{case}. Report due by {due}.",
                    "info",
                )
            )
        case EventType.REVIEW_NOT_SATISFIED:
            new = p.get("new_referral") or {}
            plans.append(
                (
                    _officers(complaint, new.get("department_id")),
                    "Your report was not accepted: please report again",
                    f"{case}. {p.get('note', '')}".strip(),
                    "warn",
                )
            )
        case EventType.PORTAL_REJECTED:
            plans.append(
                (_staff(complaint, SDM_TEAM), title, f"{case}. {p.get('reason', '')}", tone)
            )
            new = p.get("new_referral") or {}
            plans.append(
                (
                    _officers(complaint, new.get("department_id")),
                    "A complaint has been marked to you again",
                    case,
                    "warn",
                )
            )
        case EventType.PORTAL_ACCEPTED:
            plans.append((_staff(complaint, SDM_SIDE), title, case, tone))
        case EventType.REOPENED:
            plans.append(
                (_staff(complaint, SDM_TEAM), title, f"{case}. {p.get('reason', '')}", tone)
            )

    rows = []
    for users, headline, body, row_tone in plans:
        for user in users:
            if event.actor_id and user.pk == event.actor_id:
                continue
            rows.append(
                Notification(
                    office_id=complaint.office_id,
                    recipient=user,
                    complaint=complaint,
                    event=event,
                    title=headline[:250],
                    body=body.strip()[:400],
                    tone=row_tone,
                )
            )
    if rows:
        Notification.objects.bulk_create(rows)


def _count(n: int, noun: str, rest: str) -> str:
    """'3 reports are waiting for your review' / '1 report is waiting for your review'."""
    return f"{n} {noun}{'' if n == 1 else 's'} {'is' if n == 1 else 'are'} {rest}"


def unread_count(user) -> int:
    return Notification.objects.filter(recipient=user, read_at__isnull=True).count()


def attention_items(user, visible: QuerySet[Complaint]) -> list[dict[str, Any]]:
    """What needs doing right now, counted from the live data (no stored notification)."""
    today = timezone.localdate()
    items: list[dict[str, Any]] = []
    roles = user.role_names

    overdue = Referral.objects.filter(
        complaint__in=visible, closed_on__isnull=True, due_on__lt=today
    ).count()
    if overdue and roles & {Role.CLERK, Role.REVIEWER, Role.ADMIN, Role.AUDITOR}:
        items.append(
            {
                "tone": "bad",
                "text": f"{overdue} report{'s are' if overdue != 1 else ' is'} overdue",
                "url": reverse("complaints:overdue"),
            }
        )
    if overdue and Role.DEPARTMENT_OFFICER in roles:
        items.append(
            {
                "tone": "bad",
                "text": f"{overdue} of your reports {'are' if overdue != 1 else 'is'} overdue",
                "url": reverse("complaints:list"),
            }
        )
    waiting = visible.filter(current_state=State.AWAITING_REPORT).count()
    if waiting and Role.DEPARTMENT_OFFICER in roles:
        items.append(
            {
                "tone": "info",
                "text": _count(waiting, "complaint", "waiting for your report"),
                "url": reverse("complaints:list") + "?status=AWAITING_REPORT",
            }
        )
    to_review = visible.filter(current_state=State.UNDER_REVIEW).count()
    if to_review and roles & {Role.REVIEWER, Role.ADMIN}:
        items.append(
            {
                "tone": "warn",
                "text": _count(to_review, "report", "waiting for your review"),
                "url": reverse("complaints:list") + "?status=UNDER_REVIEW",
            }
        )
    return items
