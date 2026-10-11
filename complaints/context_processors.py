from django.urls import reverse

from complaints import permissions
from complaints.models import Notification
from complaints.services.notifications import unread_count

BELL_ITEMS = 6
GREET_ON = {"list", "dashboard", "overdue", "export"}


def bell_context(user) -> dict:
    """The bell's number and the few most recent notifications shown in its dropdown."""
    recent = list(
        Notification.objects.filter(recipient=user).select_related("complaint")[:BELL_ITEMS]
    )
    return {"notif_unread": unread_count(user), "notif_recent": recent}


def nav(request):
    user = request.user
    if not user.is_authenticated:
        return {}
    context = {
        "can_register": permissions.can(user, permissions.REGISTER),
        "can_analyse": permissions.can_analyse(user),
        "can_manage_logins": permissions.can(user, permissions.MANAGE_LOGINS),
        "admin_index_url": reverse("admin:index") if user.is_staff else "",
    } | bell_context(user)

    # Once per login, say what is waiting.
    match = request.resolver_match
    if (
        context["notif_unread"]
        and request.method == "GET"
        and not request.headers.get("HX-Request")
        and match
        and match.url_name in GREET_ON
        and not request.session.get("notif_greeted")
    ):
        request.session["notif_greeted"] = True
        context["notif_greeting"] = context["notif_recent"][:3]
    return context
