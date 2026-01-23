import re
from django.core.management.base import BaseCommand
from django.db import transaction

from training.models import Person


# Paste your mapping here: "Operation Name WITHOUT year" -> "Contingent"
# IMPORTANT: keys should NOT include the year (no trailing " 2025")
OP_TO_CONTINGENT = {
    "JO Bulgaria": "C1",
    "JO Georgia": "C1",
    "JO Moldova": "C1",
    "JO Romania": "C1",
    "JO Cyprus": "C2",
    "JO Greece": "C2",
    "JO Albania": "C3",
    "JO Austria": "C3",
    "JO Bosnia and Herzegovina": "C3",
    "JO Croatia": "C3",
    "JO Kosovo": "C3",
    "JO Montenegro": "C3",
    "JO North Macedonia": "C3",
    "JO Serbia": "C3",
    "JO Slovenia": "C3",
    "JO Italy": "C4",
    "JO Malta": "C4",
    "JO Portugal": "C5",
    "JO Spain": "C5",
    "JO Belgium": "C6",
    "JO Czech Republic": "C6",
    "JO Denmark": "C6",
    "JO France": "C6",
    "JO Germany": "C6",
    "JO Iceland": "C6",
    "JO Luxemburg": "C6",
    "JO Netherlands": "C6",
    "JO Switzerland": "C6",
    "JO Estonia": "C7",
    "JO Finland": "C7",
    "JO Latvia": "C7",
    "JO Lithuania": "C7",
    "JO Norway": "C7",
    "JO Poland": "C7",
    "JO Slovakia": "C7",
    "RA FOA Return": "R1",
    "PRA FOA Return": "R2",
    "OPSHF Operational Horizontal Functions SCU": "SCU",
    "IC Information Collection/Exchange and Situational Awareness": "OPC",
    "FSA Aerial Surveillance": "OPC",
    "SA Supporting FX Entities": "HQ",
    "OPSHF Operational Horizontal Functions CMU": "CMU",
    "OPSHF Document Expertise": "OSSU",
    "LOG Supporting Activities LOGISTICS": "CENT.LOG",
    "OPSHF Operational Horizontal Functions PRET": "DRET",
    "OPSHF Operational Horizontal Functions ROS": "PRET",
}

YEAR_RE = re.compile(r"\s+\d{4}\s*$")


def normalize_deployment(value: str) -> str:
    """
    Turns 'JO Latvia 2025' -> 'JO Latvia'
    Also trims extra spaces like 'LOG ... LOGISTICS  2025'
    """
    if not value:
        return ""
    s = " ".join(value.strip().split())          # collapse whitespace
    s = YEAR_RE.sub("", s).strip()               # remove trailing ' 2025'
    return s


class Command(BaseCommand):
    help = "Assign Person.contingent based on Person.current_deployment using a mapping table."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show changes without saving")
        parser.add_argument("--only-empty", action="store_true", help="Only fill contingent if empty")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        only_empty = options["only_empty"]

        updated = 0
        no_deployment = 0
        no_match = 0

        qs = Person.objects.all()
        if only_empty:
            qs = qs.filter(contingent="")

        to_update = []

        # Build a case-insensitive lookup for safety
        map_ci = {k.lower(): v for k, v in OP_TO_CONTINGENT.items()}

        for p in qs.iterator():
            dep = (p.current_deployment or "").strip()
            if not dep:
                no_deployment += 1
                continue

            op_name = normalize_deployment(dep)
            cont = map_ci.get(op_name.lower())

            if not cont:
                no_match += 1
                continue

            if p.contingent != cont:
                p.contingent = cont
                to_update.append(p)

        if dry_run:
            self.stdout.write(self.style.WARNING(f"DRY RUN: would update {len(to_update)} people."))
        else:
            with transaction.atomic():
                Person.objects.bulk_update(to_update, ["contingent"], batch_size=1000)
            updated = len(to_update)
            self.stdout.write(self.style.SUCCESS(f"Updated {updated} people."))

        self.stdout.write(
            "\nSummary:\n"
            f"  Updated: {updated if not dry_run else len(to_update)}\n"
            f"  Skipped (no deployment): {no_deployment}\n"
            f"  Skipped (no match in mapping): {no_match}\n"
        )

        if no_match:
            self.stdout.write(self.style.WARNING(
                "Tip: to see which deployments didn't match, I can add an option to print them."
            ))
