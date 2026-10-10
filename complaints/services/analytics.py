"""Office-wide numbers for the dashboard: where complaints are, who is slow, what is coming in."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Any

from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q, QuerySet
from django.db.models.functions import TruncWeek
from django.utils import timezone

from complaints.models import Complaint, PortalSubmission, Referral, State
from complaints.services.pendency import bucket_labels, days_at_current_office
from complaints.services.search import kpi_counts

RANGES = {"30": 30, "90": 90, "365": 365, "all": None}
WEEKS_SHOWN = 12
TOP_N = 8


def _days(delta) -> float | None:
    return round(delta.total_seconds() / 86400, 1) if delta is not None else None


def _rows(qs, key: str, label: str = "label") -> dict[str, list]:
    """A queryset of {key, n} dicts as parallel label/value lists for a chart."""
    items = list(qs)
    return {
        "labels": [str(i[key] or "Not stated") for i in items],
        "values": [i["n"] for i in items],
    }


def build_dashboard(visible: QuerySet[Complaint], range_key: str = "90") -> dict[str, Any]:
    today = timezone.localdate()
    days = RANGES.get(range_key, 90)
    since: date | None = today - timedelta(days=days) if days else None
    scoped = visible.filter(received_on__gte=since) if since else visible
    open_cases = scoped.exclude(current_state=State.CLOSED)
    closed_cases = scoped.filter(current_state=State.CLOSED)
    referrals = Referral.objects.filter(complaint__in=scoped)

    # ---- headline numbers
    close_time = closed_cases.filter(closed_on__isnull=False).aggregate(
        avg=Avg(ExpressionWrapper(F("closed_on") - F("received_on"), output_field=DurationField()))
    )["avg"]
    answered = referrals.filter(outcome=Referral.Outcome.REPORT_RECEIVED, closed_on__isnull=False)
    report_time = answered.aggregate(
        avg=Avg(ExpressionWrapper(F("closed_on") - F("issued_on"), output_field=DurationField()))
    )["avg"]
    on_time = answered.filter(closed_on__lte=F("due_on")).count()
    overdue_reports = Referral.objects.filter(
        complaint__in=open_cases, closed_on__isnull=True, due_on__lt=today
    ).count()
    kpi = kpi_counts(visible)  # ageing buckets are for what is open today, whatever the range
    labels = dict(bucket_labels())
    ageing = {
        "labels": [labels[b] for b in ("safe", "warning", "overdue", "critical")],
        "values": [kpi[b] for b in ("safe", "warning", "overdue", "critical")],
    }

    # ---- distributions
    by_state = _rows(
        scoped.values("current_state").annotate(n=Count("pk")).order_by("-n"), "current_state"
    )
    by_state["labels"] = [State(s).label for s in by_state["labels"]] if by_state["labels"] else []
    platform_names = dict(Complaint._meta.get_field("source_platform").choices)
    by_platform = _rows(
        scoped.values("source_platform").annotate(n=Count("pk")).order_by("-n"), "source_platform"
    )
    by_platform["labels"] = [platform_names.get(p, p) for p in by_platform["labels"]]
    by_department = _rows(
        referrals.values("to_department__short_name")
        .annotate(n=Count("complaint", distinct=True))
        .order_by("-n"),
        "to_department__short_name",
    )
    by_village = _rows(
        scoped.exclude(village__isnull=True)
        .values("village__name")
        .annotate(n=Count("pk"))
        .order_by("-n")[:TOP_N],
        "village__name",
    )
    by_category = _rows(
        scoped.exclude(category__isnull=True)
        .values("category__name")
        .annotate(n=Count("pk"))
        .order_by("-n")[:TOP_N],
        "category__name",
    )
    by_tag = _rows(
        scoped.exclude(tags__isnull=True)
        .values("tags__name")
        .annotate(n=Count("pk", distinct=True))
        .order_by("-n")[:TOP_N],
        "tags__name",
    )

    # ---- week by week
    start = today - timedelta(weeks=WEEKS_SHOWN - 1)
    start -= timedelta(days=start.weekday())
    weeks = [start + timedelta(weeks=i) for i in range(WEEKS_SHOWN)]
    received = {
        r["w"]: r["n"]
        for r in visible.filter(received_on__gte=start)
        .annotate(w=TruncWeek("received_on"))
        .values("w")
        .annotate(n=Count("pk"))
    }
    closed = {
        r["w"]: r["n"]
        for r in visible.filter(closed_on__gte=start)
        .annotate(w=TruncWeek("closed_on"))
        .values("w")
        .annotate(n=Count("pk"))
    }
    weekly = {
        "labels": [w.strftime("%-d %b") for w in weeks],
        "received": [received.get(w, 0) for w in weeks],
        "closed": [closed.get(w, 0) for w in weeks],
    }

    # ---- departments: who answers, how fast, who is behind
    perf = []
    for row in (
        referrals.values("to_department__short_name")
        .annotate(
            sent=Count("pk"),
            answered=Count("pk", filter=Q(outcome=Referral.Outcome.REPORT_RECEIVED)),
            on_time=Count(
                "pk",
                filter=Q(outcome=Referral.Outcome.REPORT_RECEIVED, closed_on__lte=F("due_on")),
            ),
            avg=Avg(
                ExpressionWrapper(F("closed_on") - F("issued_on"), output_field=DurationField()),
                filter=Q(outcome=Referral.Outcome.REPORT_RECEIVED),
            ),
            waiting=Count("pk", filter=Q(closed_on__isnull=True)),
            late=Count("pk", filter=Q(closed_on__isnull=True, due_on__lt=today)),
        )
        .order_by("-sent")
    ):
        perf.append(
            {
                "name": row["to_department__short_name"],
                "sent": row["sent"],
                "answered": row["answered"],
                "on_time_pct": round(100 * row["on_time"] / row["answered"])
                if row["answered"]
                else None,
                "avg_days": _days(row["avg"]),
                "waiting": row["waiting"],
                "late": row["late"],
            }
        )
    dept_speed = {
        "labels": [p["name"] for p in perf if p["avg_days"] is not None],
        "values": [p["avg_days"] for p in perf if p["avg_days"] is not None],
    }

    # ---- the portals: ATRs filed and how often they came back
    atr = PortalSubmission.objects.filter(complaint__in=scoped)
    portal_counts = Counter(atr.values_list("outcome", flat=True))
    submitted = sum(portal_counts.values())
    atr_stats = {
        "labels": ["Closed by portal", "Sent back", "Waiting"],
        "values": [
            portal_counts[PortalSubmission.Outcome.ACCEPTED],
            portal_counts[PortalSubmission.Outcome.REJECTED],
            portal_counts[PortalSubmission.Outcome.PENDING],
        ],
        "total": submitted,
        "sent_back_pct": round(100 * portal_counts[PortalSubmission.Outcome.REJECTED] / submitted)
        if submitted
        else None,
    }

    # ---- the ones to look at today
    oldest = sorted(
        open_cases.select_related("complainant", "village", "current_department"),
        key=lambda c: c.current_stage_started_at,
    )[:TOP_N]
    for c in oldest:
        c.days_here = days_at_current_office(c, today)

    return {
        "range_key": range_key if range_key in RANGES else "90",
        "total": scoped.count(),
        "open": open_cases.count(),
        "closed": closed_cases.count(),
        "avg_close_days": _days(close_time),
        "avg_report_days": _days(report_time),
        "on_time_pct": round(100 * on_time / answered.count()) if answered.count() else None,
        "overdue_reports": overdue_reports,
        "ageing": ageing,
        "by_state": by_state,
        "by_platform": by_platform,
        "by_department": by_department,
        "by_village": by_village,
        "by_category": by_category,
        "by_tag": by_tag,
        "weekly": weekly,
        "dept_perf": perf,
        "dept_speed": dept_speed,
        "atr": atr_stats,
        "oldest": oldest,
    }
