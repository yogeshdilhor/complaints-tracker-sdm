"""All permission checks live here: visible_complaints() and can()."""

from __future__ import annotations

from django.db.models import QuerySet

from complaints.models import Complaint, State
from complaints.workflow import Action, available_actions
from core.roles import Role

# Case actions that are not state transitions.
REGISTER = "register"
ATTACH_DOCUMENT = "attach_document"
ADD_NOTE = "add_note"
EDIT_DETAILS = "edit_details"
ADD_STAGE = "add_stage"  # a stage the user names themselves, with any documents
MANAGE_LOGINS = "manage_logins"  # create, renew and switch off officer logins

_CLERK_ACTIONS = {
    REGISTER,
    ATTACH_DOCUMENT,
    ADD_NOTE,
    ADD_STAGE,
    EDIT_DETAILS,
    Action.ISSUE_NOTICE,
    Action.REISSUE_NOTICE,
    Action.RECORD_REPORT,
    Action.SUBMIT_TO_PORTAL,
    Action.PORTAL_ACCEPTED,
    Action.PORTAL_REJECTED,
}
_REVIEWER_ACTIONS = _CLERK_ACTIONS | {
    Action.REVIEW_SATISFIED,
    Action.REVIEW_NOT_SATISFIED,
    Action.CLOSE_WITHOUT_PORTAL,
    Action.REOPEN,
}
_REVIEWER_ACTIONS |= {MANAGE_LOGINS}
_ADMIN_ACTIONS = _REVIEWER_ACTIONS  # Admin can do everything Reviewer can


_OFFICE_WIDE = {Role.CLERK, Role.REVIEWER, Role.ADMIN, Role.AUDITOR}


def _roles(user) -> set[str]:
    return user.role_names if user.is_authenticated else set()


def visible_complaints(user) -> QuerySet[Complaint]:
    """Department scoping is enforced here, in the query."""
    if not user.is_authenticated:
        return Complaint.objects.none()
    qs = Complaint.objects.all()
    roles = _roles(user)
    if not user.office_id:
        return qs.none()
    qs = qs.filter(office_id=user.office_id)
    if roles & {Role.CLERK, Role.REVIEWER, Role.ADMIN, Role.AUDITOR}:
        return qs
    if Role.DEPARTMENT_OFFICER in roles and user.department_id:
        # Every case ever marked to their office, for reference. What they may DO is far narrower
        # (see can()), and what they see inside a case is limited by services.timeline.
        return qs.filter(referrals__to_department_id=user.department_id).distinct()
    return qs.none()


def can_analyse(user) -> bool:
    """The dashboard and the table download cover the whole office, so department officers
    (who see only their own cases) are left out."""
    return bool(user.is_authenticated and user.office_id and _roles(user) & _OFFICE_WIDE)


def can(user, action: str, complaint: Complaint | None = None) -> bool:
    """May this user perform `action` (optionally on this complaint)?

    Only roles grant rights; Django's superuser flag has no special meaning here.
    Auditors are read-only.
    """
    if not user.is_authenticated:
        return False
    if complaint is not None and not visible_complaints(user).filter(pk=complaint.pk).exists():
        return False
    roles = _roles(user)
    allowed: set = set()
    if Role.CLERK in roles:
        allowed |= _CLERK_ACTIONS
    if Role.REVIEWER in roles:
        allowed |= _REVIEWER_ACTIONS
    if Role.ADMIN in roles:
        allowed |= _ADMIN_ACTIONS
    if Role.DEPARTMENT_OFFICER in roles and complaint is not None:
        # An officer can only upload their report, and only while the case is marked to them
        # and waiting for it. visible_complaints() already limited the case to their unit.
        if (
            complaint.current_state == State.AWAITING_REPORT
            and complaint.current_department_id == user.department_id
        ):
            allowed.add(Action.RECORD_REPORT)
    return action in allowed


def allowed_actions(user, complaint: Complaint) -> list[str]:
    """State-valid actions the user may take, for rendering buttons."""
    return [str(a) for a in available_actions(complaint) if can(user, a, complaint)]


def officer_can_see_document(user, document) -> bool:
    """Officers may open only the documents on the stages they can see: the complaint itself,
    notices to them, and their own reports. Everyone else with access to the case sees all."""
    if Role.DEPARTMENT_OFFICER not in _roles(user) or _roles(user) & _OFFICE_WIDE:
        return True
    from complaints.services.timeline import build_timeline

    stages = build_timeline(document.complaint, department=user.department)
    return any(document.pk == d.pk for stage in stages for d in stage.documents)
