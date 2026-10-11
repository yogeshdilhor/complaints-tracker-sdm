from __future__ import annotations

from datetime import date
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from complaints.models import Complaint, Document, Platform, State, Tag
from complaints.services.documents import UploadError, read_upload
from complaints.services.intake import normalise_phone
from complaints.services.search import BUCKET_CHOICES
from complaints.services.timeline import portal_name
from complaints.widgets import START_ON_OWN, WheelWidget
from complaints.workflow import NEXT_STEPS, REISSUE_OUTCOMES, Action
from core.models import Category, Department, Office, Village

_DATE = forms.DateInput(attrs={"type": "date"})


class AttachmentField(forms.FileField):
    """Validates size and real file type, and returns an Attachment instead of an upload."""

    def clean(self, data, initial=None):
        upload = super().clean(data, initial)
        if not upload:
            return None
        try:
            return read_upload(upload)
        except UploadError as exc:
            raise ValidationError(str(exc)) from exc


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiAttachmentField(forms.FileField):
    """Any number of files, each checked for size and real type; returns a list of Attachments."""

    widget = MultipleFileInput

    def clean(self, data, initial=None):
        uploads = data if isinstance(data, (list, tuple)) else [data]
        single = AttachmentField(required=False)
        return [a for a in (single.clean(u) for u in uploads if u) if a is not None]


def _active_departments(office: Office):
    return Department.objects.filter(office=office, is_active=True)


# ------------------------------------------------------------------ filters


class ComplaintFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    department = forms.ModelChoiceField(queryset=Department.objects.none(), required=False)
    village = forms.ModelChoiceField(queryset=Village.objects.none(), required=False)
    platform = forms.ChoiceField(choices=[("", "All platforms"), *Platform.choices], required=False)
    status = forms.ChoiceField(choices=[("", "All statuses"), *State.choices], required=False)
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False)
    tag = forms.ModelChoiceField(queryset=Tag.objects.none(), required=False)
    date_from = forms.DateField(required=False, widget=_DATE, label="From")
    date_to = forms.DateField(required=False, widget=_DATE, label="To")
    bucket = forms.ChoiceField(
        choices=[("", "All"), *[(b, b) for b in BUCKET_CHOICES]], required=False
    )

    def __init__(self, *args, office: Office | None, **kwargs):
        super().__init__(*args, **kwargs)
        if office is not None:
            self.fields["department"].queryset = Department.objects.filter(office=office)
            self.fields["village"].queryset = Village.objects.filter(office=office)
            self.fields["category"].queryset = Category.objects.filter(office=office)
            self.fields["tag"].queryset = Tag.objects.filter(office=office)
        self.fields["department"].empty_label = "All departments"
        self.fields["village"].empty_label = "All villages"
        self.fields["category"].empty_label = "All categories"
        self.fields["tag"].empty_label = "All tags"
        self.fields["q"].widget.attrs.update(
            {"placeholder": "Search ref no, phone, name, village, text...", "autocomplete": "off"}
        )

    def filters(self) -> dict[str, Any]:
        return self.cleaned_data if self.is_valid() else {}


class ExportForm(forms.Form):
    """Which complaints go into the Excel table."""

    department = forms.ModelChoiceField(
        queryset=Department.objects.none(),
        required=False,
        empty_label="All departments",
        label="Department it was marked to",
    )
    platform = forms.ChoiceField(
        choices=[("", "All portals"), *Platform.choices], required=False, label="Portal"
    )
    status = forms.ChoiceField(
        choices=[("", "Open and closed"), ("open", "Open only"), ("closed", "Closed only")],
        required=False,
    )
    archive = forms.ChoiceField(
        choices=[
            ("", "Active complaints"),
            ("only", "Filed complaints (Archive)"),
            ("both", "Active and filed"),
        ],
        required=False,
        label="Include",
    )

    def __init__(self, *args, office: Office | None, **kwargs):
        super().__init__(*args, **kwargs)
        if office is not None:
            self.fields["department"].queryset = Department.objects.filter(office=office)


# ------------------------------------------------------------------ actions


# The stage titles offered on the timeline; {officer} and {portal} are filled in as you pick.
HEADLINE_PRESETS = [
    "Complaint received",
    "Marked to {officer} for report",
    "{officer} report filed",
    "Report accepted by SDM",
    "Report found inadequate; {officer} asked to report again",
    "ATR uploaded on {portal}",
    "Complaint re-opened on {portal}",
    "Complaint closed on {portal}",
    "Resolved and filed",
    "Complaint re-opened",
    "Note added",
    "Document added",
]
# Which title is pre-selected for each action.
DEFAULT_HEADLINE = {
    "issue_notice": "Marked to {officer} for report",
    "reissue_notice": "Marked to {officer} for report",
    "record_report": "{officer} report filed",
    "review_satisfied": "Report accepted by SDM",
    "review_not_satisfied": "Report found inadequate; {officer} asked to report again",
    "submit_to_portal": "ATR uploaded on {portal}",
    "portal_accepted": "Complaint closed on {portal}",
    "portal_rejected": "Complaint re-opened on {portal}",
    "close_without_portal": "Resolved and filed",
    "reopen": "Complaint re-opened",
    "add_note": "Note added",
    "attach_document": "Document added",
}


def _headline_field(complaint: Complaint, action: str | None) -> forms.CharField:
    """A wheel of stage titles, with a way to write your own."""
    current = complaint.current_department
    return forms.CharField(
        required=False,
        max_length=200,
        label="Stage title on the timeline",
        widget=WheelWidget(
            [(t, t) for t in HEADLINE_PRESETS],
            own_placeholder="Write the title of this stage",
            selected=DEFAULT_HEADLINE.get(action or ""),
            data={
                "kind": "headline",
                "portal": portal_name(complaint),
                "officer": str(current) if current else "",
            },
        ),
    )


class ActionForm(forms.Form):
    """Base for forms that feed apply_transition().

    `transition_fields` are passed through when filled in. If `file_key` is set the
    uploaded file is stored as a Document of `file_kind` and passed under that key.
    """

    transition_fields: tuple[str, ...] = ()
    file_key: str | None = None
    file_kind: str | None = None

    def __init__(self, *args, complaint: Complaint, **kwargs):
        super().__init__(*args, **kwargs)
        self.complaint = complaint
        self._new_department = ""
        self.fields["files"] = MultiAttachmentField(
            required=False, label="More documents for this stage (any number)"
        )
        self.fields["headline"] = _headline_field(complaint, _ACTION_OF_FORM.get(type(self)))
        self.order_fields(["department", "headline"])
        if "department" in self.fields:
            self.fields["create_login"] = forms.BooleanField(
                required=False,
                label="Also create a login for this officer (valid for a month)",
            )
            self.fields["officer_name"] = forms.CharField(
                required=False,
                max_length=150,
                label="Officer's name (shown on the login, optional)",
            )
            departments = _active_departments(complaint.office)
            field = self.fields["department"]
            self._department_required = field.required
            field.queryset = departments
            field.required = False  # "Write my own" posts no id; clean() checks instead
            field.widget = WheelWidget(
                [("", "Choose...")] + [(d.pk, str(d)) for d in departments],
                own_placeholder="Type the officer or office",
                data={"kind": "officer"},
            )

    def clean(self):
        cleaned = super().clean()
        if "department" in self.fields and not cleaned.get("department"):
            own = (self.data.get("department_own") or "").strip()
            if own:
                self._new_department = own[:200]
            elif self._department_required:
                self.add_error("department", "Choose who this goes to, or write the name.")
        return cleaned

    def _department_named(self, name: str) -> Department:
        """The office's department of this name, created if it does not exist yet."""
        existing = Department.objects.filter(
            office=self.complaint.office, name__iexact=name
        ).first()
        if existing is not None:
            return existing
        return Department.objects.create(
            office=self.complaint.office,
            name=name,
            short_name=name[:50],
            kind=Department.Kind.EXTERNAL_DEPARTMENT,
        )

    def transition_data(self) -> dict[str, Any]:
        """Call inside the transaction: a typed-in officer becomes a department here."""
        data = {}
        for name in self.transition_fields:
            value = self.cleaned_data.get(name)
            if value not in (None, ""):
                data[name] = value
        if self._new_department:
            data["department"] = self._department_named(self._new_department)
        if self.cleaned_data.get("headline"):
            data["headline"] = self.cleaned_data["headline"]
        return data

    @property
    def extra_attachments(self) -> list:
        return self.cleaned_data.get("files") or []

    @property
    def attachment(self):
        return self.cleaned_data.get("file")


def _department_field(required: bool, label: str = "Department") -> forms.ModelChoiceField:
    return forms.ModelChoiceField(
        queryset=Department.objects.none(), required=required, label=label
    )


class IssueNoticeForm(ActionForm):
    transition_fields = ("department", "due_on", "remarks")
    file_key, file_kind = "notice_document", Document.Kind.NOTICE
    department = _department_field(True, "Send notice to")
    due_on = forms.DateField(required=False, widget=_DATE, help_text="Blank = set from SLA.")
    remarks = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    file = AttachmentField(required=False, label="Notice (PDF/JPG/PNG)")


class ReissueNoticeForm(IssueNoticeForm):
    transition_fields = ("outcome", "department", "due_on", "remarks")
    outcome = forms.ChoiceField(
        choices=[(o.value, o.label) for o in sorted(REISSUE_OUTCOMES)],
        label="Previous notice",
    )
    department = _department_field(False, "Department (blank = same as before)")


class RecordReportForm(ActionForm):
    transition_fields = ("received_on",)
    file_key, file_kind = "document", Document.Kind.INQUIRY_REPORT
    received_on = forms.DateField(required=False, widget=_DATE, help_text="Blank = today.")
    file = AttachmentField(required=False, label="Inquiry report (PDF/JPG/PNG, optional)")


class ReviewSatisfiedForm(ActionForm):
    transition_fields = ("note",)
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class ReviewNotSatisfiedForm(IssueNoticeForm):
    transition_fields = ("note", "department", "due_on", "remarks")
    note = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), label="Why not satisfied")
    department = _department_field(False, "Fresh notice to (blank = same department)")


class SubmitToPortalForm(ActionForm):
    transition_fields = ("submitted_on", "portal_ack_no")
    file_key, file_kind = "document", Document.Kind.ATR
    submitted_on = forms.DateField(required=False, widget=_DATE, help_text="Blank = today.")
    portal_ack_no = forms.CharField(required=False, max_length=100, label="Portal ack. no.")
    file = AttachmentField(required=False, label="ATR copy (PDF/JPG/PNG, optional)")


class PortalAcceptedForm(ActionForm):
    transition_fields = ("portal_ack_no",)
    portal_ack_no = forms.CharField(required=False, max_length=100, label="Portal ack. no.")


class PortalRejectedForm(IssueNoticeForm):
    transition_fields = ("reason", "next_step", "department", "due_on", "remarks")
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), label="Rejection reason")
    next_step = forms.ChoiceField(
        choices=[
            ("resubmit", "Fix locally and resubmit"),
            ("review_again", "SDM to review again"),
            ("new_inquiry", "Send a fresh notice"),
        ]
    )
    department = _department_field(False, "Fresh notice to (only for a fresh notice)")

    def clean_next_step(self):
        value = self.cleaned_data["next_step"]
        if value not in NEXT_STEPS:
            raise ValidationError("Unknown next step.")
        return value


class ReasonForm(ActionForm):
    transition_fields = ("reason",)
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))


class NoteForm(forms.Form):
    text = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), label="Note")

    def __init__(self, *args, complaint: Complaint, **kwargs):
        super().__init__(*args, **kwargs)
        self.complaint = complaint
        self.fields["headline"] = _headline_field(complaint, "add_note")


class NewLoginForm(forms.Form):
    """A login for an officer, made without marking a complaint."""

    department = forms.ModelChoiceField(
        queryset=Department.objects.none(), label="Office or department"
    )
    name = forms.CharField(required=False, max_length=150, label="Officer's name (optional)")
    phone = forms.CharField(required=False, max_length=20, label="Officer's phone (optional)")

    def __init__(self, *args, office: Office, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = _active_departments(office)


class FileComplaintForm(forms.Form):
    """Moving a complaint to the Archive needs a reason on record."""

    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Why is this complaint being filed?",
        help_text="It leaves the normal views and goes to the Archive. It can be restored later.",
    )

    def __init__(self, *args, complaint: Complaint, **kwargs):
        super().__init__(*args, **kwargs)
        self.complaint = complaint


class UnfileForm(forms.Form):
    reason = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Why restore it? (optional)"
    )

    def __init__(self, *args, complaint: Complaint, **kwargs):
        super().__init__(*args, **kwargs)
        self.complaint = complaint


class AddStageForm(forms.Form):
    """A stage you name yourself: a title, an optional note, and any number of documents."""

    note = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 3}), label="What happened (optional)"
    )
    files = MultiAttachmentField(required=False, label="Documents for this stage (any number)")

    def __init__(self, *args, complaint: Complaint, **kwargs):
        super().__init__(*args, **kwargs)
        self.complaint = complaint
        headline = _headline_field(complaint, None)
        headline.widget.selected = START_ON_OWN
        headline.required = True
        headline.label = "Name this stage"
        self.fields["headline"] = headline
        self.order_fields(["headline"])

    def clean_headline(self) -> str:
        value = (self.cleaned_data.get("headline") or "").strip()
        if not value:
            raise ValidationError("Give the stage a title.")
        return value


class AttachDocumentForm(forms.Form):
    kind = forms.ChoiceField(choices=Document.Kind.choices, initial=Document.Kind.OTHER)
    file = AttachmentField(required=True, label="File (PDF/JPG/PNG)")
    supersedes = forms.ModelChoiceField(
        queryset=Document.objects.none(),
        required=False,
        label="Replaces (correction of an earlier document)",
    )

    def __init__(self, *args, complaint: Complaint, **kwargs):
        super().__init__(*args, **kwargs)
        self.complaint = complaint
        self.fields["supersedes"].queryset = complaint.documents.filter(is_deleted=False)
        self.fields["headline"] = _headline_field(complaint, "attach_document")


class EditDetailsForm(forms.Form):
    complainant_name = forms.CharField(max_length=200)
    complainant_phone = forms.CharField(max_length=20, required=False)
    complainant_address = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    source_platform = forms.ChoiceField(choices=Platform.choices)
    external_ref = forms.CharField(max_length=100, required=False)
    received_on = forms.DateField(widget=_DATE)
    village = forms.ModelChoiceField(queryset=Village.objects.none(), required=False)
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False)
    subject = forms.CharField(max_length=300)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))

    def clean_complainant_phone(self) -> str:
        return normalise_phone(self.cleaned_data["complainant_phone"])

    def __init__(self, *args, complaint: Complaint, **kwargs):
        super().__init__(*args, **kwargs)
        self.complaint = complaint
        self.fields["village"].queryset = Village.objects.filter(office=complaint.office)
        self.fields["category"].queryset = Category.objects.filter(office=complaint.office)
        if not self.is_bound:
            c = complaint
            self.initial = {
                "complainant_name": c.complainant.name,
                "complainant_phone": c.complainant.phone,
                "complainant_address": c.complainant.address,
                "source_platform": c.source_platform,
                "external_ref": c.external_ref,
                "received_on": c.received_on,
                "village": c.village_id,
                "category": c.category_id,
                "subject": c.subject,
                "description": c.description,
            }


# ------------------------------------------------------------------- intake


class IntakeUploadForm(forms.Form):
    file = AttachmentField(required=True, label="Complaint (PDF, JPG or PNG, max 10 MB)")


class IntakeForm(forms.Form):
    name = forms.CharField(max_length=200, label="Complainant name")
    phone = forms.CharField(max_length=20, required=False)
    address = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    village = forms.ModelChoiceField(queryset=Village.objects.none(), required=False)
    source_platform = forms.ChoiceField(
        choices=[("", "Select platform"), *Platform.choices], label="Platform"
    )
    external_ref = forms.CharField(max_length=100, required=False, label="External reference")
    received_on = forms.DateField(widget=_DATE, initial=lambda: timezone.localdate())
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False)
    subject = forms.CharField(max_length=300)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))
    tags = forms.ModelMultipleChoiceField(queryset=Tag.objects.none(), required=False)
    new_tag = forms.CharField(max_length=50, required=False, label="New tag")
    department = forms.ModelChoiceField(
        queryset=Department.objects.none(),
        required=False,
        label="Marked to (department written on the paper)",
        empty_label="Not marked yet",
    )
    related_to = forms.ModelChoiceField(queryset=Complaint.objects.none(), required=False)
    confirm_duplicates = forms.BooleanField(required=False)

    def __init__(self, *args, office: Office, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["village"].queryset = Village.objects.filter(office=office, is_active=True)
        self.fields["category"].queryset = Category.objects.filter(office=office)
        self.fields["tags"].queryset = Tag.objects.filter(office=office)
        self.fields["department"].queryset = _active_departments(office)
        self.fields["related_to"].queryset = Complaint.objects.filter(office=office)
        self.fields["related_to"].widget = forms.HiddenInput()
        self.fields["confirm_duplicates"].widget = forms.HiddenInput()

    def clean_phone(self) -> str:
        return normalise_phone(self.cleaned_data["phone"])

    def clean_received_on(self) -> date:
        value = self.cleaned_data["received_on"]
        if value > timezone.localdate():
            raise ValidationError("Received date cannot be in the future.")
        return value


ACTION_FORMS: dict[str, type[forms.Form]] = {
    Action.ISSUE_NOTICE: IssueNoticeForm,
    Action.REISSUE_NOTICE: ReissueNoticeForm,
    Action.RECORD_REPORT: RecordReportForm,
    Action.REVIEW_SATISFIED: ReviewSatisfiedForm,
    Action.REVIEW_NOT_SATISFIED: ReviewNotSatisfiedForm,
    Action.SUBMIT_TO_PORTAL: SubmitToPortalForm,
    Action.PORTAL_ACCEPTED: PortalAcceptedForm,
    Action.PORTAL_REJECTED: PortalRejectedForm,
    Action.CLOSE_WITHOUT_PORTAL: ReasonForm,
    Action.REOPEN: ReasonForm,
}


_ACTION_OF_FORM = {form: action for action, form in ACTION_FORMS.items()}
