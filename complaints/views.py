from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from complaints import permissions as perms
from complaints.forms import (
    ACTION_FORMS,
    AddStageForm,
    AttachDocumentForm,
    ComplaintFilterForm,
    EditDetailsForm,
    ExportForm,
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
from complaints.services.analytics import build_dashboard
from complaints.services.description_summary import (
    extract_complaint_fields,
    read_document_as_stage,
    summarise_report,
)
from complaints.services.documents import DuplicateDocument, extract, store_document
from complaints.services.export import build_workbook, export_queryset
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
from complaints.services.timeline import build_timeline, portal_name
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
from core.models import Department, Village
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
        "can_auto_stage": perms.can(user, perms.ADD_STAGE, complaint),
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


def _form_for(
    action: str,
    request: HttpRequest,
    complaint: Complaint,
    *,
    initial: dict | None = None,
    unbound: bool = False,
):
    kwargs: dict[str, Any] = {"complaint": complaint}
    if initial:
        kwargs["initial"] = initial
    data = (request.POST, request.FILES) if request.method == "POST" and not unbound else ()
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


def _run_action(
    action: str, complaint: Complaint, user, form, *, staged=None, doc_summary: str = ""
) -> None:
    """Perform the action inside one transaction; raises TransitionError / DuplicateDocument.

    `staged` is a document already uploaded and read in the "upload a document" flow; it
    becomes part of this stage, with `doc_summary` (as confirmed by the user) kept on it.
    """
    data = form.cleaned_data
    with transaction.atomic():
        staged_doc = None
        if action in ACTION_FORMS:
            payload = form.transition_data()
            if form.attachment is not None:
                payload[form.file_key] = store_document(
                    complaint, form.attachment, form.file_kind, user
                )
            extras = [
                store_document(complaint, a, Document.Kind.OTHER, user)
                for a in form.extra_attachments
            ]
            if staged is not None:
                if form.file_key and form.file_key not in payload:
                    staged_doc = store_document(complaint, staged, form.file_kind, user)
                    payload[form.file_key] = staged_doc
                else:
                    staged_doc = store_document(complaint, staged, Document.Kind.OTHER, user)
                    extras.append(staged_doc)
            payload["extra_documents"] = extras
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
            if staged is not None:
                staged_doc = store_document(complaint, staged, Document.Kind.OTHER, user)
                documents.append(staged_doc)
            add_stage(complaint, user, data["headline"], data["note"], documents)
        elif action == perms.ADD_NOTE:
            add_note(complaint, user, data["text"], headline=data.get("headline", ""))
        elif action == perms.EDIT_DETAILS:
            edit_details(complaint, user, **data)
        if staged_doc is not None and doc_summary:
            Document.objects.filter(pk=staged_doc.pk).update(summary=doc_summary[:2000])


@login_required
@require_http_methods(["GET", "POST"])
def complaint_action(request: HttpRequest, pk: int, action: str) -> HttpResponse:
    complaint = _visible_complaint(request, pk)
    if action not in ACTION_LABELS:
        raise Http404
    if not perms.can(request.user, action, complaint):
        return HttpResponseForbidden("You are not allowed to do this.")

    staged = None
    if request.method == "POST" and request.POST.get("staged"):
        staged = _staged_attachment(request, complaint, request.POST["staged"])
    form = _form_for(action, request, complaint)
    if request.POST.get("staged"):
        form.fields.pop("file", None)  # the document was already uploaded
    if action == Action.RECORD_REPORT and _officer_only(request.user):
        # A department officer must attach the report itself; the date is always today.
        form.fields["file"].required = True
        form.fields["file"].label = "Your report (PDF/JPG/PNG)"
        del form.fields["received_on"]
    error = None
    if request.method == "POST" and request.POST.get("staged") and staged is None:
        error = "The uploaded document has expired. Please upload it again."
    elif request.method == "POST" and form.is_valid():
        try:
            _run_action(
                action,
                complaint,
                request.user,
                form,
                staged=staged,
                doc_summary=request.POST.get("doc_summary", "").strip(),
            )
            if staged is not None:
                request.session.pop(STAGE_SESSION_KEY, None)
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

    context = {
        "complaint": complaint,
        "form": form,
        "action": action,
        "label": ACTION_LABELS[action],
        "error": error,
        "multipart": any(f.widget.needs_multipart_form for f in form.fields.values()),
    }
    if request.POST.get("staged"):
        context |= _staged_context(
            request, complaint, action, request.POST["staged"], request.POST.get("doc_summary", "")
        )
    return render(request, "complaints/_action_form.html", context)


# -------------------------------------------- stage from an uploaded document

STAGE_SESSION_KEY = "stage_doc"


def _staged_attachment(request: HttpRequest, complaint: Complaint, sha: str):
    meta = request.session.get(STAGE_SESSION_KEY)
    if not meta or meta.get("pk") != complaint.pk or meta.get("sha") != sha:
        return None
    return intake.load_staged(sha, meta)


def _stage_type_choices(user, complaint: Complaint) -> list[tuple[str, str]]:
    allowed = perms.allowed_actions(user, complaint)
    return [(a, MENU_LABELS[a]) for a in allowed] + [
        (perms.ADD_STAGE, MENU_LABELS[perms.ADD_STAGE])
    ]


def _staged_context(request, complaint, action, sha, doc_summary) -> dict[str, Any]:
    meta = request.session.get(STAGE_SESSION_KEY) or {}
    return {
        "staged": {"sha": sha, "filename": meta.get("filename", ""), "typed": meta.get("typed")},
        "doc_summary": doc_summary,
        "stage_types": _stage_type_choices(request.user, complaint),
        "stage_type": action,
        "stage_url": reverse("complaints:stage_from_document", args=[complaint.pk]),
    }


def _match_department(name: str, office) -> Department | None:
    """The office's department an officer's title refers to ('BDPO Dabwali' -> BDPO)."""
    wanted = name.strip().casefold()
    if not wanted:
        return None
    best = None
    for dept in Department.objects.filter(office=office, is_active=True):
        for label in {dept.name.casefold(), dept.short_name.casefold()} - {""}:
            if label == wanted or label in wanted:
                if best is None or len(label) > best[0]:
                    best = (len(label), dept)
    return best[1] if best else None


def _read_date(value: str) -> date | None:
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed <= timezone.localdate() else None


def _stage_initial(action: str, ai: dict[str, str], office) -> dict[str, Any]:
    """Pre-fill the confirmation form from what the model read."""
    initial: dict[str, Any] = {}
    if ai.get("headline"):
        initial["headline"] = ai["headline"]
    when = _read_date(ai.get("date", ""))
    department = _match_department(ai.get("officer", ""), office)
    note, reason = ai.get("note", ""), ai.get("reason", "")
    if action in (Action.ISSUE_NOTICE, Action.REISSUE_NOTICE):
        initial |= {"department": department.pk} if department else {}
        initial["remarks"] = note
    elif action == Action.REVIEW_NOT_SATISFIED:
        initial |= {"department": department.pk} if department else {}
        initial["note"] = reason or note
    elif action == Action.RECORD_REPORT and when:
        initial["received_on"] = when
    elif action == Action.REVIEW_SATISFIED:
        initial["note"] = note
    elif action == Action.SUBMIT_TO_PORTAL:
        initial |= {"submitted_on": when} if when else {}
        initial["portal_ack_no"] = ai.get("ack_no", "")
    elif action == Action.PORTAL_ACCEPTED:
        initial["portal_ack_no"] = ai.get("ack_no", "")
    elif action == Action.PORTAL_REJECTED:
        initial |= {"department": department.pk} if department else {}
        initial |= {"reason": reason or note, "next_step": "review_again"}
    elif action in (Action.CLOSE_WITHOUT_PORTAL, Action.REOPEN):
        initial["reason"] = reason or note
    elif action == perms.ADD_STAGE:
        initial["note"] = note
    return initial


@login_required
@require_http_methods(["GET", "POST"])
def stage_from_document(request: HttpRequest, pk: int) -> HttpResponse:
    """Upload a new document, let the model read it, and confirm the stage before it is logged."""
    complaint = _visible_complaint(request, pk)
    if not perms.can(request.user, perms.ADD_STAGE, complaint):
        return HttpResponseForbidden("You are not allowed to do this.")
    _decorate(complaint)
    upload_page = {"complaint": complaint}
    types = _stage_type_choices(request.user, complaint)
    valid = {key for key, _ in types}
    meta = request.session.get(STAGE_SESSION_KEY)

    if request.method == "POST":
        form = IntakeUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, "complaints/_stage_upload.html", upload_page | {"form": form})
        attachment = form.cleaned_data["file"]
        extraction = extract(attachment.content, attachment.mime_type)
        suggestion: dict[str, str] = {}
        if extraction.text and extraction.typed is not False and llm.is_configured():
            current = complaint.current_department
            suggestion = read_document_as_stage(
                extraction.text,
                {
                    "subject": complaint.subject,
                    "state": complaint.get_current_state_display(),
                    "department": str(current) if current else "the SDM office",
                    "portal": portal_name(complaint),
                },
                [key for key, _ in types],
            )
        meta = {
            "pk": complaint.pk,
            "sha": intake.stage_upload(attachment),
            "filename": attachment.filename,
            "extracted_text": extraction.text,
            "text_status": extraction.status,
            "page_count": extraction.pages,
            "typed": extraction.typed,
            "suggestion": suggestion,
        }
        request.session[STAGE_SESSION_KEY] = meta
        chosen = suggestion.get("action") or perms.ADD_STAGE
    else:
        chosen = request.GET.get("type", "")
        if not meta or meta.get("pk") != complaint.pk or not chosen:
            return render(request, "complaints/_stage_upload.html", upload_page)
        if chosen not in valid:
            chosen = perms.ADD_STAGE

    if chosen not in valid:
        chosen = perms.ADD_STAGE
    ai = meta.get("suggestion") or {}
    form = _form_for(
        chosen,
        request,
        complaint,
        initial=_stage_initial(chosen, ai, complaint.office),
        unbound=True,
    )
    form.fields.pop("file", None)
    context = {
        "complaint": complaint,
        "form": form,
        "action": chosen,
        "label": "Check what was read",
        "error": None,
        "multipart": any(f.widget.needs_multipart_form for f in form.fields.values()),
        "unread": meta.get("typed") is False or not meta.get("extracted_text"),
        "ai_used": bool(ai),
    } | _staged_context(request, complaint, chosen, meta["sha"], ai.get("summary", ""))
    return render(request, "complaints/_action_form.html", context)


# ----------------------------------------------------- dashboard and downloads


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    if not perms.can_analyse(request.user):
        return HttpResponseForbidden("The dashboard covers the whole office.")
    data = build_dashboard(perms.visible_complaints(request.user), request.GET.get("range", "90"))
    chart_keys = (
        "weekly", "by_state", "by_platform", "by_department", "by_village",
        "by_category", "by_tag", "ageing", "dept_speed", "atr",
    )  # fmt: skip
    return render(
        request,
        "complaints/dashboard.html",
        {
            "d": data,
            "charts": {k: data[k] for k in chart_keys},
            "ranges": [
                ("30", "30 days"),
                ("90", "90 days"),
                ("365", "1 year"),
                ("all", "All time"),
            ],
        },
    )


def _export_selection(request: HttpRequest):
    form = ExportForm(request.GET or None, office=request.user.office)
    chosen = form.cleaned_data if form.is_valid() else {}
    queryset = export_queryset(
        perms.visible_complaints(request.user),
        department=chosen.get("department"),
        platform=chosen.get("platform", ""),
        status=chosen.get("status", ""),
    )
    return form, queryset


@login_required
def export_page(request: HttpRequest) -> HttpResponse:
    if not perms.can_analyse(request.user):
        return HttpResponseForbidden("The table download covers the whole office.")
    form, queryset = _export_selection(request)
    return render(
        request,
        "complaints/export.html",
        {"form": form, "count": queryset.count(), "querystring": request.GET.urlencode()},
    )


@login_required
def export_download(request: HttpRequest) -> HttpResponse:
    if not perms.can_analyse(request.user):
        return HttpResponseForbidden("The table download covers the whole office.")
    _, queryset = _export_selection(request)
    response = HttpResponse(
        build_workbook(queryset),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    stamp = timezone.localdate().isoformat()
    response["Content-Disposition"] = f'attachment; filename="sdm-complaints-{stamp}.xlsx"'
    return response


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
