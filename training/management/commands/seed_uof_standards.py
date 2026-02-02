from django.core.management.base import BaseCommand
from training.models import UseOfForceStandard

def time_to_seconds(value: str) -> int:
    """
    Accepts: "4.40" (PDF format) OR "4:40" OR "04:40"
    Returns integer seconds.
    """
    v = (value or "").strip()
    if not v:
        return 0

    # PDF uses dot as separator (e.g. 4.40 means 4 min 40 sec)
    if "." in v and v.replace(".", "").isdigit():
        parts = v.split(".")
        if len(parts) == 2:
            m = int(parts[0])
            s = int(parts[1])
            return m * 60 + s

    if ":" in v:
        parts = v.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            m = int(parts[0])
            s = int(parts[1])
            return m * 60 + s

    # fallback: treat as seconds if pure digits
    if v.isdigit():
        return int(v)

    raise ValueError(f"Invalid time format: {value}")

class Command(BaseCommand):
    help = "Seed Use of Force standards (men/women) from PDF values."

    def handle(self, *args, **options):
        MEN = UseOfForceStandard.GENDER_MALE
        WOMEN = UseOfForceStandard.GENDER_FEMALE

        # Age groups in the same order as your model
        AGES = [
            UseOfForceStandard.AGE_UNDER_30,
            UseOfForceStandard.AGE_30_34,
            UseOfForceStandard.AGE_35_39,
            UseOfForceStandard.AGE_40_44,
            UseOfForceStandard.AGE_45_49,
            UseOfForceStandard.AGE_50_54,
            UseOfForceStandard.AGE_55_59,
            UseOfForceStandard.AGE_60_PLUS,
        ]

        # ----------------------------
        # DATA FROM PDF
        # ----------------------------
        # MEN (CAT1)
        men_pushups = [32, 30, 28, 26, 25, 22, 19, 14]
        men_pushups_good = [41, 39, 37, 35, 33, 29, 26, 21]
        men_pushups_vg = [50, 48, 47, 44, 40, 35, 33, 28]

        men_situps = [40, 38, 35, 33, 29, 25, 23, 20]
        men_situps_good = [45, 43, 40, 38, 34, 29, 27, 23]
        men_situps_vg = [52, 48, 45, 43, 39, 33, 31, 27]

        men_run = ["4.40", "4.50", "5.00", "5.10", "5.20", "5.35", "5.50", "6.00"]
        men_run_good = ["4.20", "4.30", "4.40", "4.50", "5.00", "5.20", "5.35", "5.45"]
        men_run_vg = ["3.55", "4.05", "4.15", "4.25", "4.40", "5.00", "5.20", "5.35"]

        # WOMEN (CAT1)
        women_pushups = [15, 13, 11, 9, 8, 5, 4, 3]
        women_pushups_good = [21, 19, 17, 15, 13, 11, 8, 5]
        women_pushups_vg = [25, 23, 21, 18, 16, 14, 10, 8]

        women_situps = [36, 34, 31, 29, 26, 22, 20, 17]
        women_situps_good = [40, 38, 35, 33, 30, 26, 24, 20]
        women_situps_vg = [47, 45, 42, 40, 37, 33, 31, 25]

        women_run = ["5.20", "5.30", "5.40", "5.50", "6.00", "6.15", "6.30", "6.50"]
        women_run_good = ["5.05", "5.15", "5.25", "5.35", "5.50", "6.00", "6.20", "6.40"]
        women_run_vg = ["4.45", "5.00", "5.05", "5.20", "5.35", "5.50", "6.10", "6.25"]

        def upsert(gender, exercise, mins, goods, vgs, is_run=False):
            created = 0
            updated = 0
            for idx, age in enumerate(AGES):
                defaults = {
                    "minimum": time_to_seconds(mins[idx]) if is_run else int(mins[idx]),
                    "good": time_to_seconds(goods[idx]) if is_run else int(goods[idx]),
                    "very_good": time_to_seconds(vgs[idx]) if is_run else int(vgs[idx]),
                }
                obj, was_created = UseOfForceStandard.objects.update_or_create(
                    gender=gender,
                    exercise=exercise,
                    age_group=age,
                    defaults=defaults
                )
                if was_created:
                    created += 1
                else:
                    updated += 1
            return created, updated

        total_created = total_updated = 0

        # Men
        c,u = upsert(MEN, UseOfForceStandard.EXERCISE_PUSHUPS, men_pushups, men_pushups_good, men_pushups_vg, is_run=False); total_created+=c; total_updated+=u
        c,u = upsert(MEN, UseOfForceStandard.EXERCISE_SITUPS,  men_situps,  men_situps_good,  men_situps_vg, is_run=False); total_created+=c; total_updated+=u
        c,u = upsert(MEN, UseOfForceStandard.EXERCISE_RUN,     men_run,     men_run_good,     men_run_vg, is_run=True); total_created+=c; total_updated+=u

        # Women
        c,u = upsert(WOMEN, UseOfForceStandard.EXERCISE_PUSHUPS, women_pushups, women_pushups_good, women_pushups_vg, is_run=False); total_created+=c; total_updated+=u
        c,u = upsert(WOMEN, UseOfForceStandard.EXERCISE_SITUPS,  women_situps,  women_situps_good,  women_situps_vg, is_run=False); total_created+=c; total_updated+=u
        c,u = upsert(WOMEN, UseOfForceStandard.EXERCISE_RUN,     women_run,     women_run_good,     women_run_vg, is_run=True); total_created+=c; total_updated+=u

        self.stdout.write(self.style.SUCCESS(
            f"Done. created={total_created}, updated={total_updated}"
        ))
