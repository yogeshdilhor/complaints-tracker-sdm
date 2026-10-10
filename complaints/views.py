from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from complaints import permissions as perms
from complaints.forms import (
    ACTION_FORMS,
    AddStageForm,
    AttachDocumentForm,
    ComplaintFilterForm,
    EditDetailsForm,
    IntakeForm,
    IntakeUploadForm,
    NoteForm,
)
from complaints.models import (
    Complainant,
    Complaint,
    Document,
    PortalSubmission,
    Referral,
    State,
    Tag,
)
from complaints.services import intake, llm
from complaints.services.description_summary import extract_complaint_fields, summarise_report
from complaints.services.documents import DuplicateDocument, extract, store_document
from complaints.services.pendency import (
    BUCKET_ORDER,
    ageing_bucket,
    bucket_labels,
    current_location,
    days_at_current_office,
    total_age_days,
)
from complaints.services.search import filter_complaints, kpi_counts, paginate
from complaints.services.summary import build_summary
from complaints.services.timeline import build_timeline
from complaints.workflow import (
    Action,
    TransitionError,
    add_note,
    add_stage,
    apply_transition,
    attach_document,
    edit_details,
    register_complaint,
)
from core.models import Village
from core.roles import Role

ACTION_LABELS = {
    Action.ISSUE_NOTICE: "Issue notice",
    Action.REISSUE_NOTICE: "Re-issue notice",
    Action.RECORD_REPORT: "Record report received",
    Action.REVIEW_SATISFIED: "Mark satisfied",
    Action.REVIEW_NOT_SATISFIED: "Mark not satisfied",
    Action.SUBMIT_TO_PORTAL: "Record portal submission",
    Action.PORTAL_ACCEPTED: "Portal accepted",
    Action.PORTAL_REJECTED: "Portal rejected",
    Action.CLOSE_WITHOUT_PORTAL: "Close without portal",
    Action.REOPEN: "Reopen",
    perms.ATTACH_DOCUMENT: "Attach document",
    perms.ADD_NOTE: "Add note",
    perms.EDIT_DETAILS: "Edit details",
    perms.ADD_STAGE: "Add a stage",
}
# What the "+" menu on the timeline offers, in plain words.
MENU_LABELS = {
    Action.ISSUE_NOTICE: "Mark to an officer for report",
    Action.REISSUE_NOTICE: "Ask for the report again",
    Action.RECORD_REPORT: "Record a report received",
    Action.REVIEW_SATISFIED: "Accept the report",
    Action.REVIEW_NOT_SATISFIED: "Report inadequate: ask again",
    Action.SUBMIT_TO_PORTAL: "ATR uploaded on the portal",
    Action.PORTAL_ACCEPTED: "Portal closed the complaint",
    Action.PORTAL_REJECTED: "Portal re-opened the complaint",
    Action.CLOSE_WITHOUT_PORTAL: "Resolve and file",
    Action.REOPEN: "Re-open the complaint",
    perms.ADD_STAGE: "Custom stage: name it yourself",
    perms.EDIT_DETAILS: "Correct complainant details",
}
NON_STATE_ACTIONS = (perms.ADD_STAGE, perms.EDIT_DETAILS)


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _visible_complaint(request: HttpRequest, pk: int) -> Complaint:
    qs = perms.visible_complaints(request.user).select_related(
        "complainant", "village", "category", "current_department", "office"
    )
    return get_object_or_404(qs, pk=pk)


def _decorate(complaint: Complaint) -> Complaint:
    """Attach display values so templates need no logic."""
    complaint.days_pending = days_at_current_office(complaint)
    complaint.bucket = ageing_bucket(complaint)
    complaint.location = current_location(complaint)
    return complaint


# --------------------------------------------------------------------- list


def _kpi_items(kpi: dict[str, int]) -> list[dict[str, Any]]:
    """The stat strip, most severe first; the ageing ones filter the list when clicked."""
    labels = dict(bucket_labels())
    items = [{"label": "Total cases", "count": kpi["total"], "tone": "", "bucket": None}]
    items.append({"label": "Pending", "count": kpi["pending"], "tone": "pending", "bucket": None})
    for bucket in reversed(BUCKET_ORDER):
        items.append(
            {"label": labels[bucket], "count": kpi[bucket], "tone": bucket, "bucket": bucket}
        )
    items.append({"label": "Closed", "count": kpi["closed"], "tone": "closed", "bucket": "closed"})
    return items


@login_required
def complaint_list(request: HttpRequest) -> HttpResponse:
    visible = perms.visible_complaints(request.user)
    form = ComplaintFilterForm(request.GET or None, office=request.user.office)
    filters = form.filters()
    qs = filter_complaints(
        visible.select_related("complainant", "village", "current_department"), filters
    )
    page = paginate(qs, request.GET.get("page"))
    for complaint in page:
        _decorate(complaint)

    params = request.GET.copy()
    params.pop("page", None)
    context = {
        "form": form,
        "page": page,
        "kpi": kpi_counts(visible),
        "kpi_items": _kpi_items(kpi_counts(visible)),
        "oob": _is_htmx(request),
        "querystring": params.urlencode(),
        "buckets": [("", "All"), *bucket_labels()],
        "active_bucket": filters.get("bucket", ""),
        "can_register": perms.can(request.user, perms.REGISTER),
    }
    template = "complaints/_list.html" if _is_htmx(request) else "complaints/list.html"
    return render(request, template, context)


# ------------------------------------------------------------------- detail


def _detail_context(request: HttpRequest, complaint: Complaint, **extra: Any) -> dict[str, Any]:
    _decorate(complaint)
    user = request.user
    state_actions = perms.allowed_actions(user, complaint)
    actions = [(a, MENU_LABELS[a]) for a in state_actions]
    actions += [(a, MENU_LABELS[a]) for a in NON_STATE_ACTIONS if perms.can(user, a, complaint)]
    open_referral = complaint.referrals.filter(closed_on__isnull=True).first()
    return {
        "complaint": complaint,
        "summary": build_summary(complaint),
        "timeline": build_timeline(complaint),
        "actions": actions,
        "total_age": total_age_days(complaint),
        "open_referral": open_referral,
        "referral_overdue": bool(open_referral and open_referral.due_on < timezone.localdate()),
        **extra,
    }


@login_required
def complaint_detail(request: HttpRequest, pk: int) -> HttpResponse:
    complaint = _visible_complaint(request, pk)
    context = _detail_context(request, complaint)
    if _is_htmx(request):
        return render(request, "complaints/_detail.html", context)
    return render(request, "complaints/detail.html", context)


# ------------------------------------------------------------------ actions


def _form_for(action: str, request: HttpRequest, complaint: Complaint):
    kwargs = {"complaint": complaint}
    data = (request.POST, request.FILES) if request.method == "POST" else ()
    if action in ACTION_FORMS:
        return ACTION_FORMS[action](*data, **kwargs)
    forms_by_name = {
        perms.ATTACH_DOCUMENT: AttachDocumentForm,
        perms.ADD_NOTE: NoteForm,
        perms.ADD_STAGE: AddStageForm,
        perms.EDIT_DETAILS: EditDetailsForm,
    }
    return forms_by_name[action](*data, **kwargs)


def _officer_only(user) -> bool:
    return bool(user.role_names & {Role.DEPARTMENT_OFFICER}) and not (
        user.role_names & {Role.CLERK, Role.REVIEWER, Role.ADMIN}
    )


def _run_action(action: str, complaint: Complaint, user, form) -> None:
    """Perform the action inside one transaction; raises TransitionError / DuplicateDocument."""
    data = form.cleaned_data
    with transaction.atomic():
        if action in ACTION_FORMS:
            payload = form.transition_data()
            if form.attachment is not None:
                payload[form.file_key] = store_document(
                    complaint, form.attachment, form.file_kind, user
                )
            payload["extra_documents"] = [
                store_document(complaint, a, Document.Kind.OTHER, user)
                for a in form.extra_attachments
            ]
            apply_transition(complaint, action, user, **payload)
        elif action == perms.ATTACH_DOCUMENT:
            attach_document(
                complaint,
                user,
                data["file"],
                data["kind"],
                supersedes=data["supersedes"],
                headline=data.get("headline", ""),
            )
        elif action == perms.ADD_STAGE:
            documents = [
                store_document(complaint, a, Document.Kind.OTHER, user) for a in data["files"]
            ]
            add_stage(complaint, user, data["headline"], data["note"], documents)
        elif action == perms.ADD_NOTE:
            add_note(complaint, user, data["text"], headline=data.get("headline", ""))
        elif action == perms.EDIT_DETAILS:
            edit_details(complaint, user, **data)


@login_required
@require_http_methods(["GET", "POST"])
def complaint_action(request: HttpRequest, pk: int, action: str) -> HttpResponse:
    complaint = _visible_complaint(request, pk)
    if action not in ACTION_LABELS:
        raise Http404
    if not perms.can(request.user, action, complaint):
        return HttpResponseForbidden("You are not allowed to do this.")

    form = _form_for(action, request, complaint)
    if action == Action.RECORD_REPORT and _officer_only(request.user):
        # A department officer must attach the report itself; the date is always today.
        form.fields["file"].required = True
        form.fields["file"].label = "Your report (PDF/JPG/PNG)"
        del form.fields["received_on"]
    error = None
    if request.method == "POST" and form.is_valid():
        try:
            _run_action(action, complaint, request.user, form)
        except DuplicateDocument as exc:
            error = f"{exc} ({exc.existing.original_filename})"
        except TransitionError as exc:
            error = str(exc)
        else:
            if not _is_htmx(request):
                return redirect("complaints:detail", pk=pk)
            if not perms.visible_complaints(request.user).filter(pk=pk).exists():
                # The action handed the case on (an officer's report), so it is no longer theirs.
                response = render(request, "complaints/_handed_on.html", {"complaint": complaint})
                response["HX-Trigger"] = "complaintChanged"
                response["HX-Retarget"] = "#detail-pane"
                response["HX-Reswap"] = "innerHTML"
                return response
            complaint = _visible_complaint(request, pk)
            response = render(
                request, "complaints/_detail.html", _detail_context(request, complaint)
            )
            response["HX-Trigger"] = "complaintChanged"
            response["HX-Retarget"] = "#detail-pane"
            response["HX-Reswap"] = "innerHTML"
            return response

    return render(
        request,
        "complaints/_action_form.html",
        {
            "complaint": complaint,
            "form": form,
            "action": action,
            "label": ACTION_LABELS[action],
            "error": error,
            "multipart": any(f.widget.needs_multipart_form for f in form.fields.values()),
        },
    )


# ---------------------------------------------------------------- documents


@login_required
def document_download(request: HttpRequest, pk: int) -> FileResponse:
    """Files are only ever served here, after a visibility check; there is no public media URL."""
    document = get_object_or_404(Document, pk=pk, is_deleted=False)
    if not perms.visible_complaints(request.user).filter(pk=document.complaint_id).exists():
        raise Http404
    response = FileResponse(
        document.file.open("rb"),
        content_type=document.mime_type,
        as_attachment=request.GET.get("download") == "1",
        filename=document.original_filename,
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_http_methods(["POST"])
def document_summary(request: HttpRequest, pk: int) -> HttpResponse:
    """A 50-75 word summary of one document, made on the first click and then kept."""
    document = get_object_or_404(Document, pk=pk, is_deleted=False)
    if not perms.visible_complaints(request.user).filter(pk=document.complaint_id).exists():
        raise Http404
    error = ""
    if not document.summary:
        if not document.extracted_text:  # reports are uploaded without being read; read now
            with document.file.open("rb") as handle:
                extraction = extract(handle.read(), document.mime_type)
            Document.objects.filter(pk=document.pk).update(
                extracted_text=extraction.text,
                text_status=extraction.status,
                page_count=document.page_count or extraction.pages,
                is_typed=extraction.typed,
            )
            document.extracted_text, document.is_typed = extraction.text, extraction.typed
        if not document.extracted_text.strip():
            error = "No text could be read from this document."
        elif document.is_typed is False:
            error = "This looks handwritten, so it is not summarised. Please read the original."
        elif not llm.is_configured():
            error = "Summaries are switched off (no API key is set for the chosen provider)."
        else:
            summary = summarise_report(document.extracted_text, document.get_kind_display())
            if summary:
                Document.objects.filter(pk=document.pk).update(summary=summary)
                document.summary = summary
            else:
                error = "The summary could not be made just now. Please try again."
    return render(
        request,
        "complaints/_document_summary.html",
        {"document": document, "error": error},
    )


# ------------------------------------------------------------------- intake

INTAKE_SESSION_KEY = "intake"


@login_required
@require_http_methods(["GET", "POST"])
def intake_upload(request: HttpRequest) -> HttpResponse:
    if not perms.can(request.user, perms.REGISTER):
        return HttpResponseForbidden("You are not allowed to register complaints.")
    form = IntakeUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        attachment = form.cleaned_data["file"]
        extraction = extract(attachment.content, attachment.mime_type)
        sha = intake.stage_upload(attachment)
        request.session[INTAKE_SESSION_KEY] = {
            "sha": sha,
            "filename": attachment.filename,
            "extracted_text": extraction.text,
            "latin_text": extraction.latin_text,
            "text_status": extraction.status,
            "page_count": extraction.pages,
            "typed": extraction.typed,
        }
        return redirect("complaints:intake_review")
    return render(request, "complaints/intake_upload.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
def intake_review(request: HttpRequest) -> HttpResponse:
    if not perms.can(request.user, perms.REGISTER):
        return HttpResponseForbidden("You are not allowed to register complaints.")
    meta = request.session.get(INTAKE_SESSION_KEY)
    attachment = intake.load_staged(meta["sha"], meta) if meta else None
    if attachment is None:
        return redirect("complaints:intake_upload")

    office = request.user.office
    guess = intake.guess_fields(attachment.extracted_text, office, meta.get("latin_text", ""))
    if request.method == "POST":
        form = IntakeForm(request.POST, office=office)
    else:
        # Ask the model once per staged file; a page refresh must not call the API again.
        # Handwritten letters are not sent to the model: it cannot read them reliably.
        if attachment.typed is not False and meta.get("ai_for") != meta["sha"]:
            village_names = list(
                Village.objects.filter(office=office, is_active=True).values_list("name", flat=True)
            )
            ai = extract_complaint_fields(attachment.extracted_text, village_names)
            if ai:
                meta["ai"], meta["ai_for"] = ai, meta["sha"]
                request.session[INTAKE_SESSION_KEY] = meta
        intake.merge_ai_fields(guess, meta.get("ai", {}), office)
        form = IntakeForm(initial=guess.as_initial(), office=office)

    duplicates: list = []
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        if not cd["confirm_duplicates"]:
            duplicates = intake.find_possible_duplicates(
                office,
                external_ref=cd["external_ref"],
                phone=cd["phone"],
                subject=cd["subject"],
                village=cd["village"],
            )
        if not duplicates:
            complaint = _save_intake(request, cd, attachment, office)
            request.session.pop(INTAKE_SESSION_KEY, None)
            return redirect("complaints:detail", pk=complaint.pk)
        # Same page again, now with the warning; the clerk chooses how to continue.
        form.data = form.data.copy()
        form.data["confirm_duplicates"] = "on"

    return render(
        request,
        "complaints/intake_review.html",
        {
            "form": form,
            "duplicates": duplicates,
            "filename": attachment.filename,
            "text_status": attachment.text_status,
            "handwritten": attachment.typed is False,
            "village_hint": guess.village_text if guess.village_id is None else "",
            "document_preview": attachment.extracted_text[:2000],
        },
    )


def _save_intake(request: HttpRequest, cd: dict[str, Any], attachment, office) -> Complaint:
    with transaction.atomic():
        complainant = None
        if cd["phone"]:
            complainant = Complainant.objects.filter(office=office, phone=cd["phone"]).first()
        if complainant is None:
            complainant = Complainant.objects.create(
                office=office,
                name=cd["name"],
                phone=cd["phone"],
                address=cd["address"],
                village=cd["village"],
            )
        tags = list(cd["tags"])
        if cd["new_tag"]:
            tag = Tag.objects.filter(office=office, name__iexact=cd["new_tag"]).first()
            tags.append(tag or Tag.objects.create(office=office, name=cd["new_tag"]))
        complaint = register_complaint(
            office=office,
            complainant=complainant,
            actor=request.user,
            source_platform=cd["source_platform"],
            received_on=cd["received_on"],
            subject=cd["subject"],
            description=cd["description"],
            external_ref=cd["external_ref"],
            village=cd["village"],
            category=cd["category"],
            tags=tags,
            related_to=cd["related_to"],
            attachment=attachment,
        )
        if cd["department"]:  # marking it starts the department's reply clock
            apply_transition(
                complaint, Action.ISSUE_NOTICE, request.user, department=cd["department"]
            )
        return complaint


# ------------------------------------------------------------------ overdue


@login_required
def overdue_today(request: HttpRequest) -> HttpResponse:
    today = timezone.localdate()
    visible = perms.visible_complaints(request.user).select_related(
        "complainant", "village", "current_department", "category"
    )

    late = (
        visible.filter(
            current_state=State.AWAITING_REPORT,
            referrals__closed_on__isnull=True,
            referrals__due_on__lt=today,
        )
        .annotate(
            due_on=Subquery(
                Referral.objects.filter(complaint=OuterRef("pk"), closed_on__isnull=True).values(
                    "due_on"
                )[:1]
            )
        )
        .order_by("current_stage_started_at")
    )
    by_department: dict[str, list[Complaint]] = defaultdict(list)
    for complaint in late:
        _decorate(complaint)
        complaint.days_overdue = (today - complaint.due_on).days
        by_department[str(complaint.current_department)].append(complaint)

    latest_outcome = Subquery(
        PortalSubmission.objects.filter(complaint=OuterRef("pk"))
        .order_by("-attempt_no")
        .values("outcome")[:1]
    )
    rejected = (
        visible.annotate(latest_outcome=latest_outcome)
        .filter(
            latest_outcome=PortalSubmission.Outcome.REJECTED,
            current_state__in=[State.APPROVED, State.UNDER_REVIEW],
        )
        .order_by("current_stage_started_at")
    )
    rejected = [_decorate(c) for c in rejected]

    # No SLA applies at the SDM office itself, so the 'overdue' ageing threshold is used.
    sdm_days = settings.AGEING_THRESHOLDS["overdue"]
    rejected_ids = {c.pk for c in rejected}
    at_office = (
        visible.filter(
            current_state__in=[State.RECEIVED, State.UNDER_REVIEW, State.APPROVED],
            current_stage_started_at__date__lte=today - timedelta(days=sdm_days),
        )
        .exclude(pk__in=rejected_ids)
        .order_by("current_stage_started_at")
    )
    at_office = [_decorate(c) for c in at_office]

    return render(
        request,
        "complaints/overdue.html",
        {
            "by_department": dict(by_department),
            "rejected": rejected,
            "at_office": at_office,
            "sdm_days": sdm_days,
            "today": today,
        },
    )
