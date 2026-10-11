"""The "Table download": complaints, report summaries and timelines as an Excel workbook."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.utils import timezone

from complaints.models import Complaint, Document, Referral, State
from complaints.services.pendency import days_at_current_office, total_age_days
from complaints.services.timeline import build_timeline
from complaints.services.xlsx import Sheet, write_workbook


def export_queryset(
    visible: QuerySet[Complaint],
    *,
    department=None,
    platform: str = "",
    status: str = "",
    archive: str = "",
) -> QuerySet[Complaint]:
    """Complaints ever marked to `department`, from `platform`, open or closed.

    `visible` must include filed complaints; `archive` picks active (""), filed ("only") or
    both ("both").
    """
    qs = visible.select_related("complainant", "village", "category", "current_department")
    if archive == "only":
        qs = qs.filter(filed_at__isnull=False)
    elif archive != "both":
        qs = qs.filter(filed_at__isnull=True)
    if department is not None:
        qs = qs.filter(referrals__to_department=department).distinct()
    if platform:
        qs = qs.filter(source_platform=platform)
    if status == "open":
        qs = qs.exclude(current_state=State.CLOSED)
    elif status == "closed":
        qs = qs.filter(current_state=State.CLOSED)
    return qs.order_by("received_on", "id")


def _day(value) -> str:
    return value.isoformat() if value else ""


def build_workbook(complaints: QuerySet[Complaint]) -> bytes:
    complaints = list(complaints.prefetch_related("tags"))
    today = timezone.localdate()
    summary_rows: list[list[Any]] = []
    report_rows: list[list[Any]] = []
    timeline_rows: list[list[Any]] = []
    document_rows: list[list[Any]] = []

    for c in complaints:
        referrals = list(
            Referral.objects.filter(complaint=c)
            .select_related("to_department", "report", "report__document")
            .order_by("round_no")
        )
        latest_summary = ""
        reports = 0
        for ref in referrals:
            report = getattr(ref, "report", None)
            document = report.document if report else None
            if report:
                reports += 1
                if document and document.summary:
                    latest_summary = document.summary
            received = ref.closed_on if ref.outcome == Referral.Outcome.REPORT_RECEIVED else None
            report_rows.append(
                [
                    c.ref_no,
                    c.complainant.name,
                    str(ref.to_department),
                    ref.round_no,
                    _day(ref.issued_on),
                    _day(ref.due_on),
                    _day(received),
                    (received - ref.issued_on).days if received else "",
                    ("Yes" if received <= ref.due_on else "No") if received else "",
                    ref.get_outcome_display() or "Waiting for report",
                    document.summary if document else "",
                    document.original_filename if document else "",
                    report.get_review_outcome_display() if report else "",
                    report.review_note if report else "",
                ]
            )
        open_ref = next((r for r in referrals if r.closed_on is None), None)
        summary_rows.append(
            [
                c.ref_no,
                c.get_source_platform_display(),
                c.external_ref,
                _day(c.received_on),
                c.get_current_state_display(),
                str(c.current_department) if c.current_department else "SDM office",
                days_at_current_office(c, today) if c.is_open else "",
                total_age_days(c),
                _day(open_ref.due_on) if open_ref else "",
                _day(c.closed_on),
                c.complainant.name,
                c.complainant.phone,
                c.complainant.address,
                str(c.village) if c.village else "",
                str(c.category) if c.category else "",
                ", ".join(t.name for t in c.tags.all()),
                c.subject,
                c.description,
                ", ".join(dict.fromkeys(str(r.to_department) for r in referrals)),
                reports,
                latest_summary,
                timezone.localtime(c.filed_at).strftime("%Y-%m-%d") if c.filed_at else "",
                c.filing_reason,
            ]
        )
        for number, stage in enumerate(build_timeline(c), start=1):
            names = ", ".join(d.original_filename for d in stage.documents)
            timeline_rows.append(
                [
                    c.ref_no,
                    number,
                    timezone.localtime(stage.at).strftime("%Y-%m-%d %H:%M"),
                    stage.title,
                    stage.detail,
                    (stage.actor.get_full_name() or stage.actor.username) if stage.actor else "",
                    names,
                ]
            )
            for d in stage.documents:
                document_rows.append(_document_row(c, number, stage.title, d))

    return write_workbook(
        [
            Sheet(
                "Complaints",
                [
                    "Ref no",
                    "Portal",
                    "Portal ref no",
                    "Received on",
                    "Status",
                    "Currently with",
                    "Days at current office",
                    "Total age (days)",
                    "Reply due",
                    "Closed on",
                    "Complainant",
                    "Phone",
                    "Address",
                    "Village",
                    "Category",
                    "Tags",
                    "Subject",
                    "Complaint summary",
                    "Marked to (all)",
                    "Reports filed",
                    "Latest report summary",
                ],
                summary_rows,
                [
                    14,
                    16,
                    16,
                    12,
                    16,
                    18,
                    10,
                    10,
                    12,
                    12,
                    20,
                    14,
                    26,
                    16,
                    14,
                    18,
                    34,
                    60,
                    22,
                    9,
                    60,
                    12,
                    34,
                ],
            ),
            Sheet(
                "Reports",
                [
                    "Ref no",
                    "Complainant",
                    "Marked to",
                    "Round",
                    "Marked on",
                    "Due by",
                    "Report filed on",
                    "Days taken",
                    "On time",
                    "Outcome",
                    "Report summary",
                    "Report file",
                    "SDM verdict",
                    "SDM note",
                ],
                report_rows,
                [14, 20, 18, 7, 12, 12, 12, 8, 8, 18, 70, 24, 14, 30],
            ),
            Sheet(
                "Timeline",
                ["Ref no", "Stage no", "Date", "Stage", "Details", "By", "Documents"],
                timeline_rows,
                [14, 8, 17, 44, 50, 18, 40],
            ),
            Sheet(
                "Documents",
                ["Ref no", "Stage no", "Stage", "File", "Type", "Uploaded", "Summary"],
                document_rows,
                [14, 8, 36, 28, 18, 17, 80],
            ),
        ]
    )


def _document_row(c: Complaint, number: int, stage: str, d: Document) -> list[Any]:
    return [
        c.ref_no,
        number,
        stage,
        d.original_filename,
        d.get_kind_display(),
        timezone.localtime(d.uploaded_at).strftime("%Y-%m-%d %H:%M"),
        d.summary,
    ]
