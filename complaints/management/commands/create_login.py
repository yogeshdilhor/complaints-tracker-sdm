"""Create (or reset) a login for office staff: the SDM, the complaints branch, a clerk."""

from __future__ import annotations

import secrets

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from core.models import Office, User
from core.roles import Role

_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
STAFF_ROLES = [Role.ADMIN, Role.REVIEWER, Role.CLERK, Role.AUDITOR]


class Command(BaseCommand):
    help = (
        "Create a staff login and print its password once. Admin (the SDM) may also file "
        "complaints; Reviewer (the complaints branch) has every other ability."
    )

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--role", required=True, choices=[r.value for r in STAFF_ROLES])
        parser.add_argument("--name", default="", help="Name shown at the top of the page")
        parser.add_argument("--office-code", default="", help="Only needed with several offices")
        parser.add_argument("--admin-site", action="store_true", help="Also allow /admin/")
        parser.add_argument("--password", default="", help="Leave out to generate one")
        parser.add_argument("--reset", action="store_true", help="Reset an existing login")

    def handle(self, *args, **opts):
        offices = Office.objects.all()
        if opts["office_code"]:
            offices = offices.filter(code=opts["office_code"])
        if offices.count() != 1:
            raise CommandError(f"Say which office with --office-code (found {offices.count()}).")
        office = offices.get()

        username = opts["username"]
        existing = User.objects.filter(username=username).first()
        if existing and not opts["reset"]:
            raise CommandError(f"'{username}' already exists. Use --reset for a new password.")
        password = opts["password"] or "".join(secrets.choice(_ALPHABET) for _ in range(12))

        user = existing or User(username=username)
        user.office = office
        user.is_active = True
        user.login_expires_on = None
        user.is_staff = bool(opts["admin_site"])
        if opts["name"]:
            user.first_name = opts["name"]
        user.set_password(password)
        user.save()
        user.groups.set([Group.objects.get(name=opts["role"])])

        self.stdout.write(self.style.SUCCESS(f"Login ready for {office.name}"))
        self.stdout.write(f"  Username: {username}")
        self.stdout.write(f"  Password: {password}")
        self.stdout.write(f"  Role:     {opts['role']}")
        self.stdout.write(
            "Change the password after the first sign-in (top right: Change password)."
        )
