from django.db import models
from django.contrib.postgres.fields import DateTimeRangeField
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.indexes import GistIndex
from django.core.exceptions import ValidationError
from psycopg.types.range import Range
from django.db.models import Q
from django.db.models.constraints import CheckConstraint
from django.db.models import F
from django.conf import settings
from django.utils import timezone
from datetime import timedelta


class Person(models.Model):
    sysper_id = models.BigIntegerField(unique=True, db_index=True)
    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80, db_index=True)

    email = models.EmailField(blank=True)
    dob = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, blank=True)
    category = models.CharField(max_length=50, blank=True)
    current_deployment = models.CharField(max_length=120, blank=True)
    contingent = models.CharField(max_length=20, blank=True, default="")
    is_active = models.BooleanField(default=True)


    def __str__(self):
        return f"{self.last_name} {self.first_name} ({self.sysper_id})"
    
class Subject(models.Model):
    name = models.CharField(max_length=120, unique=True)

    is_recurring = models.BooleanField(default=False)
    validity_days = models.PositiveIntegerField(default=365)

    def __str__(self):
        return self.name



class TrainerSkill(models.Model):
    trainer = models.ForeignKey("Person", on_delete=models.CASCADE)
    subject = models.ForeignKey("Subject", on_delete=models.CASCADE)

    class Meta:
        unique_together = [("trainer", "subject")]

    def __str__(self):
        return f"{self.trainer} -> {self.subject}"


class Training(models.Model):
    course_name = models.CharField(max_length=255)
    subject = models.ForeignKey("Subject", on_delete=models.PROTECT, null=True, blank=True)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    location = models.CharField(max_length=120)
    capacity = models.PositiveIntegerField(default=0)
    remarks = models.TextField(blank=True)
    uof_instructor_1 = models.CharField(max_length=120, blank=True, default="")
    uof_instructor_2 = models.CharField(max_length=120, blank=True, default="")
    uof_chairman = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        constraints = [
            models.CheckConstraint(
            condition=Q(end_at__gt=F("start_at")),
            name="training_end_after_start",
            )
        ]

    def __str__(self):
        return f"{self.course_name} ({self.start_at:%Y-%m-%d})"


class Participation(models.Model):
    ROLE_CHOICES = [
        ("TRAINEE", "Trainee"),
        ("TRAINER", "Trainer"),
    ]

    STATUS_CHOICES = (
        ("AUTHORISED", "Authorised"),
        ("PENDING", "Pending"),
        ("REJECTED", "Rejected"),
        ("WITHDRAWN", "Withdrawn"),
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="PENDING", db_index=True)
    person = models.ForeignKey("Person", on_delete=models.CASCADE)
    training = models.ForeignKey("Training", on_delete=models.CASCADE)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    days = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    timespan = DateTimeRangeField(null=True, blank=True)

    # ---- Audit fields for status changes ----
    status_changed_at = models.DateTimeField(null=True, blank=True)
    status_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="status_changes"
    )
    status_comment = models.TextField(blank=True, default="")   # "why changed"

    # ---- POS section ----
    COMPLETION_CHOICES = (
        ("", "—"),
        ("PASS", "Pass"),
        ("FAIL", "Fail"),
    )
    completion_status = models.CharField(max_length=10, choices=COMPLETION_CHOICES, blank=True, default="")
    feedback = models.TextField(blank=True, default="")
    pos_comment = models.TextField(blank=True, default="")

    class Meta:
        unique_together = [("person", "training", "role")]
        indexes = [
            GistIndex(fields=["timespan"]),  # ✅ only timespan
        ]
        constraints = [
            ExclusionConstraint(
                name="no_overlapping_participation_per_person",
                expressions=[
                    ("person", "="),
                    ("timespan", "&&"),
                ],
            )
        ]
    def clean(self):
        if not self.person_id or not self.training_id:
            return

        qs = (
            Participation.objects
            .filter(person=self.person)
            .exclude(pk=self.pk)
            .filter(
                training__start_at__lt=self.training.end_at,
                training__end_at__gt=self.training.start_at,
            )
            .select_related("training")
        )

        if qs.exists():
            conflict = qs.first().training
            raise ValidationError(
                f"Overlap: {self.person} already has '{conflict.course_name}' "
                f"({conflict.start_at} - {conflict.end_at})"
            )

   
            

    def save(self, *args, **kwargs):
        # ✅ Always sync range from training dates
        self.timespan = Range(self.training.start_at, self.training.end_at, bounds='[)')
        self.full_clean()
        super().save(*args, **kwargs)


class EmailVerification(models.Model):
    email = models.EmailField(db_index=True)
    code = models.CharField(max_length=10)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    def is_expired(self):
        return self.created_at < timezone.now() - timedelta(minutes=15)

    def mark_used(self):
        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])

    def __str__(self):
        return f"{self.email} ({self.code})"
    





class UseOfForceStandard(models.Model):
    # ---- Gender ----
    GENDER_MALE = "M"
    GENDER_FEMALE = "F"
    GENDER_CHOICES = [
        (GENDER_MALE, "Men"),
        (GENDER_FEMALE, "Women"),
    ]

    # ---- Exercises ----
    EXERCISE_PUSHUPS = "PUSHUPS"
    EXERCISE_SITUPS = "SITUPS"
    EXERCISE_RUN = "RUN"

    EXERCISE_CHOICES = [
        (EXERCISE_PUSHUPS, "Push-ups"),
        (EXERCISE_SITUPS, "Sit-ups"),
        (EXERCISE_RUN, "1000m run"),
    ]

    # ---- Age groups ----
    AGE_UNDER_30 = "U29"
    AGE_30_34 = "30_34"
    AGE_35_39 = "35_39"
    AGE_40_44 = "40_44"
    AGE_45_49 = "45_49"
    AGE_50_54 = "50_54"
    AGE_55_59 = "55_59"
    AGE_60_PLUS = "60_PLUS"

    AGE_GROUP_CHOICES = [
        (AGE_UNDER_30, "Up to 29"),
        (AGE_30_34, "30–34"),
        (AGE_35_39, "35–39"),
        (AGE_40_44, "40–44"),
        (AGE_45_49, "45–49"),
        (AGE_50_54, "50–54"),
        (AGE_55_59, "55–59"),
        (AGE_60_PLUS, "60 and over"),
    ]

    AGE_SORT = {
        AGE_UNDER_30: 1,
        AGE_30_34: 2,
        AGE_35_39: 3,
        AGE_40_44: 4,
        AGE_45_49: 5,
        AGE_50_54: 6,
        AGE_55_59: 7,
        AGE_60_PLUS: 8,
    }

    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, default=GENDER_MALE)

    exercise = models.CharField(max_length=20, choices=EXERCISE_CHOICES)
    age_group = models.CharField(max_length=20, choices=AGE_GROUP_CHOICES)

    # IMPORTANT: default avoids makemigrations asking questions
    age_sort = models.PositiveSmallIntegerField(default=999)

    # For push-ups/sit-ups: integer repetitions.
    # For 1000m run: store seconds (int) so we can display as MM:SS.
    minimum = models.IntegerField(default=0)
    good = models.IntegerField(default=0)
    very_good = models.IntegerField(default=0)

    def save(self, *args, **kwargs):
        self.age_sort = self.AGE_SORT.get(self.age_group, 999)
        super().save(*args, **kwargs)

    class Meta:
        ordering = ["gender", "exercise", "age_sort"]
        unique_together = ("gender", "exercise", "age_group")

    def __str__(self):
        return f"{self.get_gender_display()} {self.get_exercise_display()} {self.get_age_group_display()}"


class UofRating(models.TextChoices):
    FAIL = "FAIL", "Fail"
    MINIMUM = "MINIMUM", "Minimum"
    GOOD = "GOOD", "Good"
    VERY_GOOD = "VERY_GOOD", "Very good"

class UofAssessment(models.Model):
    participation = models.OneToOneField(
        "Participation",
        on_delete=models.CASCADE,
        related_name="uof_assessment",
    )

    tested_at = models.DateField(default=timezone.now)

    pushups = models.PositiveSmallIntegerField(null=True, blank=True)
    situps = models.PositiveSmallIntegerField(null=True, blank=True)
    run_seconds = models.PositiveSmallIntegerField(null=True, blank=True)  # store as seconds

    pushups_rating = models.CharField(max_length=20, choices=UofRating.choices, blank=True, default="")
    situps_rating = models.CharField(max_length=20, choices=UofRating.choices, blank=True, default="")
    run_rating = models.CharField(max_length=20, choices=UofRating.choices, blank=True, default="")

    passed = models.BooleanField(default=False)

    notes = models.TextField(blank=True, default="")

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"UOF Assessment for {self.participation_id}"