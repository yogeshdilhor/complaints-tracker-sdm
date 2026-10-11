"""Remove the demo/test data so real complaints can be loaded.

Removes every complaint and everything hanging off it, the demo logins and the placeholder
offices, villages and categories. Keeps the office itself and the real staff logins
(by default `sdm` and `complaintsbranch`). Without --yes it only shows what it would remove.
"""

from __future__ import annotations

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from complaints.models import (
    Complainant,
    Complaint,
    Document,
    Event,
    InquiryReport,
    Notification,
    PortalSubmission,
    Referral,
    Tag,
)
from core.models import Category, Department, User, Village

DEMO_USERS = ["clerk", "officer", "reviewer", "admin", "auditor"]


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Really delete")
        parser.add_argument(
            "--keep-user", action="append", default=["sdm", "complaintsbranch"],
            help="A login to keep (repeatable)",
        )  # fmt: skip

    def handle(self, *args, **opts):
        keep = set(opts["keep_user"])
        users = User.objects.filter(username__in=DEMO_USERS).exclude(username__in=keep)
        plan = [
            ("notifications", Notification.objects.count()),
            ("documents (and their files)", Document.objects.count()),
            ("events", Event.objects.count()),
            ("complaints", Complaint.objects.count()),
            ("complainants", Complainant.objects.count()),
            ("tags", Tag.objects.count()),
            ("departments", Department.objects.count()),
            ("villages", Village.objects.count()),
            ("categories", Category.objects.count()),
            ("demo logins", users.count()),
        ]
        for label, n in plan:
            self.stdout.write(f"  {label}: {n}")
        self.stdout.write("Kept: the office and the logins " + ", ".join(sorted(keep)) + ".")
        if not opts["yes"]:
            self.stdout.write(self.style.WARNING("Nothing deleted. Run again with --yes."))
            return

        paths = list(Document.objects.values_list("file", flat=True))
        with transaction.atomic():
            Notification.objects.all().delete()
            PortalSubmission.objects.all().delete()
            InquiryReport.objects.all().delete()
            Referral.objects.all().delete()
            # Events are append-only inside the app, so a bulk clean-up has to bypass that.
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {Event._meta.db_table}")
            Document.objects.update(supersedes=None)
            Document.objects.all().delete()
            Complaint.objects.update(related_to=None)
            for complaint in Complaint.objects.all():
                complaint.tags.clear()
            Complaint.objects.all().delete()
            Complainant.objects.all().delete()
            Tag.objects.all().delete()
            users.delete()
            Department.objects.all().delete()
            Village.objects.all().delete()
            Category.objects.all().delete()

        removed = 0
        for path in set(paths):
            if path and default_storage.exists(path):
                default_storage.delete(path)
                removed += 1
        self.stdout.write(
            self.style.SUCCESS(f"Test data removed ({removed} stored files deleted).")
        )
