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

    person = models.ForeignKey("Person", on_delete=models.CASCADE)
    training = models.ForeignKey("Training", on_delete=models.CASCADE)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    days = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    timespan = DateTimeRangeField(null=True, blank=True)

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