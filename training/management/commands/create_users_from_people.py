from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import transaction

from training.models import Person


class Command(BaseCommand):
    help = "Create non-staff Django users from Person records (email-based)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would happen without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        User = get_user_model()

        people = Person.objects.all().order_by("sysper_id")

        created = 0
        skipped_no_email = 0
        skipped_duplicate_email = 0
        skipped_already_exists = 0

        # Track duplicates inside Person table
        seen_emails = set()

        # Preload existing user emails (case-insensitive handling)
        existing_emails = set(
            e.lower()
            for e in User.objects.exclude(email__isnull=True).exclude(email="").values_list("email", flat=True)
        )

        new_users = []

        for p in people:
            email = (p.email or "").strip().lower()

            if not email:
                skipped_no_email += 1
                continue

            # skip duplicate emails within Person list
            if email in seen_emails:
                skipped_duplicate_email += 1
                continue
            seen_emails.add(email)

            # skip if user already exists with that email
            if email in existing_emails:
                skipped_already_exists += 1
                continue

            username = f"sysper_{p.sysper_id}"

            u = User(
                username=username,
                email=email,
                is_staff=False,
                is_superuser=False,
                is_active=True,
            )
            # Important: no password yet (fits your “email code” registration later)
            u.set_unusable_password()

            new_users.append(u)

        if dry_run:
            self.stdout.write(self.style.WARNING(f"DRY RUN: would create {len(new_users)} users."))
        else:
            with transaction.atomic():
                User.objects.bulk_create(new_users, batch_size=1000)
            created = len(new_users)
            self.stdout.write(self.style.SUCCESS(f"Created {created} users."))

        self.stdout.write(
            "\nSummary:\n"
            f"  Created: {created if not dry_run else len(new_users)}\n"
            f"  Skipped (no email): {skipped_no_email}\n"
            f"  Skipped (duplicate email in Person): {skipped_duplicate_email}\n"
            f"  Skipped (user already exists): {skipped_already_exists}\n"
        )
