"""Officer logins: made on the spot when a case is marked to an officer, valid for a month."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import Group
from django.db.models import Q, QuerySet
from django.utils import timezone

from core.models import Department, Office, User
from core.roles import Role

# No 0/O, 1/l/I: a password read out over the phone must not be misheard.
_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PASSWORD_LENGTH = 10


class LoginLimitReached(Exception):
    pass


@dataclass(frozen=True)
class Credentials:
    """Shown to the SDM once; the password is never stored in readable form."""

    username: str
    password: str
    department: str
    expires_on: str  # ISO date
    name: str = ""


def officer_logins(office: Office) -> QuerySet[User]:
    return User.objects.filter(office=office, groups__name=Role.DEPARTMENT_OFFICER).select_related(
        "department"
    )


def active_logins(office: Office) -> QuerySet[User]:
    """Officer logins that can sign in today: switched on and not past their date."""
    today = timezone.localdate()
    return (
        officer_logins(office)
        .filter(is_active=True)
        .filter(Q(login_expires_on__isnull=True) | Q(login_expires_on__gte=today))
    )


def check_room(office: Office) -> None:
    limit = settings.MAX_OFFICER_LOGINS
    if limit and active_logins(office).count() >= limit:
        raise LoginLimitReached(
            f"The limit of {limit} active officer logins has been reached. "
            "Switch one off, or wait for one to expire, to make room."
        )


def _new_password() -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def _username_for(department: Department) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", department.short_name.lower()).strip("-")[:20] or "officer"
    taken = set(User.objects.filter(username__startswith=base).values_list("username", flat=True))
    if base not in taken:
        return base
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


def _expiry():
    return timezone.localdate() + timedelta(days=settings.OFFICER_LOGIN_DAYS)


def create_officer_login(department: Department, name: str = "", phone: str = "") -> Credentials:
    """A login that sees only cases marked to `department` and can only upload a report."""
    check_room(department.office)
    password = _new_password()
    user = User.objects.create_user(
        username=_username_for(department),
        password=password,
        office=department.office,
        department=department,
        first_name=(name.strip() or str(department))[:150],
        phone=phone.strip()[:20],
        login_expires_on=_expiry(),
    )
    user.groups.add(Group.objects.get(name=Role.DEPARTMENT_OFFICER))
    return Credentials(
        user.username, password, str(department), user.login_expires_on.isoformat(), user.first_name
    )


def reset_password(user: User) -> Credentials:
    """A new password for an officer who lost theirs; also gives them a fresh month."""
    password = _new_password()
    user.set_password(password)
    user.login_expires_on = _expiry()
    user.is_active = True
    user.save()
    return Credentials(
        user.username,
        password,
        str(user.department),
        user.login_expires_on.isoformat(),
        user.first_name,
    )


def extend(user: User) -> None:
    """Another month from today (or from the current end date if that is later)."""
    start = max(timezone.localdate(), user.login_expires_on or timezone.localdate())
    user.login_expires_on = start + timedelta(days=settings.OFFICER_LOGIN_DAYS)
    user.save(update_fields=["login_expires_on"])


def switch_off(user: User) -> None:
    user.is_active = False
    user.save(update_fields=["is_active"])


def switch_on(office: Office, user: User) -> None:
    check_room(office)
    user.is_active = True
    if user.is_login_expired:
        user.login_expires_on = _expiry()
    user.save(update_fields=["is_active", "login_expires_on"])
