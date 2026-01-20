import random
from datetime import datetime, timedelta, time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from training.models import Person, Training, Participation, Subject


SYSPER_IDS = [
    # (keep your list exactly as you pasted it)
    90426736, 90295462, 90334672, 90415720, 90388276, 90306234, 90295320, 90397925,
    90249163, 90415114, 90415177, 90349279, 90412483, 90318139, 90295522, 90295568,
    90350207, 90295571, 90328120, 90392336, 90295657, 90427739, 90301271, 90428947,
    90302095, 90301984, 90349475, 90373590, 90415747, 90333994, 90415274, 90341541,
    90333657, 90377816, 90343822, 90401071, 90431631, 90349290, 90295286, 90295644,
    90415062, 90333992, 90334047, 90432487, 90295577, 90377253, 90322189, 90301388,
    90301180, 90434142, 40682872, 90434559, 90249752, 90398561, 90322460, 90397806,
    90295284, 90401624, 90318299, 90306021, 90332914, 90443549, 90412319, 90333663,
    90434147, 90429296, 90429294, 90295639, 90295666, 90317914, 90412610, 90332898,
    90313323, 90333775, 90283521, 90295470, 90317615, 90418140, 90295647, 90322036,
    90349478, 90295599, 90295398, 90318154, 90302038, 90377278, 90295488, 90408644,
    90334045, 90431900, 90373491, 90412596, 90289495, 90443803, 90301127, 90427122,
    90247786, 90266796, 90426751, 90350317, 90372863, 90306963, 90434023, 90309324,
    90398563, 90250023, 90426773, 90318494, 90334044, 90317607, 90401273, 90412569,
    90301304, 90322026, 90349968, 90316477, 90377980, 90432984, 90415113, 90426918,
    90427318, 90418066, 90442820, 90295479, 90370105, 90401615, 90333660, 90333554,
    90306003, 90415669, 90431419, 90433279, 90433000, 90443534, 90428501, 90295407,
    90306253, 90295255, 90249087, 90333784, 90431890, 90415249, 90333003, 90414961,
    90415118, 90295653, 90350431, 90301414, 90415515, 90318296, 90318513, 90373338,
    90334670, 90350300, 90248242, 90377470, 90286300, 90412686, 90415138, 90295661,
    90415724, 90322106, 90316318, 90401274, 90415680, 90432850, 90333824, 90379952,
    90301176, 90415278, 90317961, 90274087, 90397965, 90414941, 90305993, 90415282,
    90428601, 90333564, 90381582, 90397071,
]


class Command(BaseCommand):
    help = "Seed dummy completed 'Use of force' training history with random dates in the last year."

    def add_arguments(self, parser):
        parser.add_argument("--days-back", type=int, default=365)
        parser.add_argument("--min-duration", type=int, default=1)
        parser.add_argument("--max-duration", type=int, default=4)
        parser.add_argument("--location", type=str, default="HQ")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        days_back = options["days_back"]
        min_dur = max(1, options["min_duration"])
        max_dur = max(min_dur, options["max_duration"])
        location = options["location"]
        dry_run = options["dry_run"]

        today = timezone.localdate()
        start_window = today - timedelta(days=days_back)

        # ✅ This matches your models: Training.subject is FK to Subject(name)
        use_of_force_subject, _ = Subject.objects.get_or_create(name="Use of force")

        def make_aware(dt: datetime):
            if timezone.is_aware(dt):
                return dt
            return timezone.make_aware(dt, timezone.get_current_timezone())

        def overlaps_existing(person: Person, start_dt, end_dt) -> bool:
            # Avoid Participation.clean overlap rule (and exclusion constraint) by checking first
            return Participation.objects.filter(
                person=person,
                training__start_at__lt=end_dt,
                training__end_at__gt=start_dt,
            ).exists()

        created_people = 0
        created_trainings = 0
        created_participations = 0
        skipped_existing_uof = 0
        skipped_overlap_fail = 0

        ctx = transaction.atomic() if not dry_run else _noop_context()
        with ctx:
            for sysper_id in SYSPER_IDS:
                person, p_created = Person.objects.get_or_create(
                    sysper_id=sysper_id,
                    defaults={
                        "first_name": "Dummy",
                        "last_name": f"User{sysper_id}",
                        "email": "",
                        "is_active": True,
                    },
                )
                if p_created:
                    created_people += 1

                # ✅ Check if they already have a Use of force within the window
                existing_uof = Participation.objects.filter(
                    person=person,
                    role="TRAINEE",
                    training__subject=use_of_force_subject,
                    training__end_at__date__gte=start_window,
                    training__end_at__date__lte=today,
                ).exists()

                if existing_uof:
                    skipped_existing_uof += 1
                    continue

                # Pick random dates within the last year; retry a few times to avoid overlaps
                made = False
                for _ in range(20):
                    dur_days = random.randint(min_dur, max_dur)

                    # start_date such that end_date <= today - 1 day (completed)
                    max_start = today - timedelta(days=dur_days)
                    if max_start < start_window:
                        max_start = start_window

                    start_date = start_window + timedelta(
                        days=random.randint(0, max(0, (max_start - start_window).days))
                    )
                    end_date = start_date + timedelta(days=dur_days - 1)

                    start_dt = make_aware(datetime.combine(start_date, time(8, 0)))
                    end_dt = make_aware(datetime.combine(end_date, time(16, 0)))

                    if overlaps_existing(person, start_dt, end_dt):
                        continue

                    training = Training.objects.create(
                        course_name="Use of force",
                        subject=use_of_force_subject,   # ✅ FK
                        location=location,
                        start_at=start_dt,
                        end_at=end_dt,
                        capacity=10,
                        remarks="Seeded dummy data",
                    )
                    created_trainings += 1

                    Participation.objects.create(
                        training=training,
                        person=person,
                        role="TRAINEE",
                        days=dur_days,
                    )
                    created_participations += 1
                    made = True
                    break

                if not made:
                    skipped_overlap_fail += 1

            if dry_run:
                raise Exception("DRY RUN complete (rolling back).")

        self.stdout.write(self.style.SUCCESS("Done."))
        self.stdout.write(
            f"People created: {created_people}\n"
            f"Trainings created: {created_trainings}\n"
            f"Participations created: {created_participations}\n"
            f"Skipped (already had Use of force in window): {skipped_existing_uof}\n"
            f"Skipped (couldn't find non-overlapping date): {skipped_overlap_fail}"
        )


class _noop_context:
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): return True
