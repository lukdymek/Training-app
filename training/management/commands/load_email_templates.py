from django.core.management.base import BaseCommand
from django.db import transaction

from training.models import EmailTemplate


DEFAULT_TEMPLATES = [
    {
        "kind": "PARTICIPANT",
        "name": "Participant Assigned",
        "subject": "Training assignment: {training_name}",
        "body": (
            "Dear {first_name},\n\n"
            "You have been assigned to {training_name} on {training_date}.\n"
            "Location: {location}.\n\n"
            "Kind regards,\nTraining Team"
        ),
        "is_active": True,
    },
    {
        "kind": "PARTICIPANT",
        "name": "Participant Status Update",
        "subject": "Training status update: {training_name}",
        "body": (
            "Dear {first_name},\n\n"
            "Your status for {training_name} has been updated to {status}.\n\n"
            "Kind regards,\nTraining Team"
        ),
        "is_active": True,
    },
    {
        "kind": "ADMIN",
        "name": "Admin Training Summary",
        "subject": "Summary: {training_name}",
        "body": (
            "Hello,\n\n"
            "Please find the training summary for {training_name}.\n"
            "Date: {training_date}\n"
            "Location: {location}\n\n"
            "Regards,\nTraining Team"
        ),
        "is_active": True,
    },
]


class Command(BaseCommand):
    help = "Load default email templates into EmailTemplate."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview actions without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        created = 0
        updated = 0

        with transaction.atomic():
            for tpl in DEFAULT_TEMPLATES:
                if dry_run:
                    exists = EmailTemplate.objects.filter(name=tpl["name"]).exists()
                    if exists:
                        updated += 1
                    else:
                        created += 1
                    continue

                _, was_created = EmailTemplate.objects.update_or_create(
                    name=tpl["name"],
                    defaults={
                        "kind": tpl["kind"],
                        "subject": tpl["subject"],
                        "body": tpl["body"],
                        "is_active": tpl["is_active"],
                    },
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

            if dry_run:
                transaction.set_rollback(True)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN: would create {created} template(s), update {updated} template(s)."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. created={created}, updated={updated}"
            )
        )
