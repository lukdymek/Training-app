from django import forms
from .models import Training
from django.core.exceptions import ValidationError
from django.db.models import Q
from psycopg.types.range import Range
from .models import Training, Participation
from .models import Person
from .models import UseOfForceStandard

class TrainingForm(forms.ModelForm):
    class Meta:
        model = Training
        fields = ["course_name", "subject", "start_at", "end_at", "location", "capacity", "remarks", "uof_iteration"]
        widgets = {
            "start_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "end_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_at")
        end = cleaned.get("end_at")

        if start and end and end <= start:
            raise forms.ValidationError("End date/time must be after start date/time.")

        if self.instance and self.instance.pk and start and end:
            new_range = Range(start, end, bounds='[)')

            person_ids = list(
                Participation.objects.filter(training=self.instance)
                .values_list("person_id", flat=True)
                .distinct()
            )

            if person_ids:
                conflicts = (
                    Participation.objects.filter(person_id__in=person_ids)
                    .exclude(training=self.instance)
                    .filter(timespan__overlap=new_range)
                    .select_related("person", "training")
                    .order_by("person_id", "timespan")
                )

                if conflicts.exists():
                    c = conflicts.first()
                    raise ValidationError(
                        f"Change not allowed: would create an overlap for {c.person} "
                        f"with '{c.training.course_name}' ({c.training.start_at} → {c.training.end_at})."
                    )
        capacity = cleaned.get("capacity")
        if self.instance and self.instance.pk and capacity is not None:
            current_trainees = Participation.objects.filter(
                training=self.instance,
                role="TRAINEE",
            ).count()

            if capacity < current_trainees:
                raise ValidationError(
                    f"Capacity cannot be set to {capacity} because there are already "
                    f"{current_trainees} trainees assigned."
                )
        return cleaned
    
class AddMultipleTrainersForm(forms.Form):
        trainers = forms.ModelMultipleChoiceField(
            queryset=Person.objects.none(),
            required=False,
            widget=forms.CheckboxSelectMultiple,
            label="Available trainers",
        )

        def __init__(self, *args, queryset=None, **kwargs):
            super().__init__(*args, **kwargs)
            if queryset is not None:
                self.fields["trainers"].queryset = queryset


class ParticipationEditForm(forms.ModelForm):
    def __init__(self, *args, lock_completion=False, **kwargs):
        super().__init__(*args, **kwargs)
        if lock_completion:
            self.fields["completion_status"].disabled = True

    class Meta:
        model = Participation
        fields = ["status_comment", "completion_status", "feedback", "pos_comment"]
        widgets = {
            "status_comment": forms.Textarea(attrs={"rows": 3}),
            "feedback": forms.Textarea(attrs={"rows": 3}),
            "pos_comment": forms.Textarea(attrs={"rows": 3}),
        }


def mmss_to_seconds(value: str) -> int:
    value = (value or "").strip()
    if not value:
        raise forms.ValidationError("This field is required.")
    if ":" not in value:
        raise forms.ValidationError("Use MM:SS format, e.g. 04:20")

    mm, ss = value.split(":", 1)
    if not (mm.isdigit() and ss.isdigit()):
        raise forms.ValidationError("Use MM:SS format, e.g. 04:20")

    mm = int(mm)
    ss = int(ss)
    if ss < 0 or ss > 59:
        raise forms.ValidationError("Seconds must be 00–59")

    return mm * 60 + ss


def seconds_to_mmss(seconds: int) -> str:
    if seconds is None:
        return ""
    mm = seconds // 60
    ss = seconds % 60
    return f"{mm:02d}:{ss:02d}"


class UseOfForceStandardForm(forms.ModelForm):
    minimum_display = forms.CharField(required=True)
    good_display = forms.CharField(required=True)
    very_good_display = forms.CharField(required=True)

    class Meta:
        model = UseOfForceStandard
        fields = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        inst = self.instance
        if inst and inst.pk:
            if inst.exercise == UseOfForceStandard.EXERCISE_RUN:

                self.fields["minimum_display"].initial = seconds_to_mmss(inst.minimum)
                self.fields["good_display"].initial = seconds_to_mmss(inst.good)
                self.fields["very_good_display"].initial = seconds_to_mmss(inst.very_good)
            else:
                self.fields["minimum_display"].initial = str(inst.minimum)
                self.fields["good_display"].initial = str(inst.good)
                self.fields["very_good_display"].initial = str(inst.very_good)

    def clean(self):
        cleaned = super().clean()
        inst = self.instance

        min_v = cleaned.get("minimum_display")
        good_v = cleaned.get("good_display")
        vg_v = cleaned.get("very_good_display")

        if inst.exercise == UseOfForceStandard.EXERCISE_RUN:
            minimum = mmss_to_seconds(min_v)
            good = mmss_to_seconds(good_v)
            very_good = mmss_to_seconds(vg_v)

            if not (minimum >= good >= very_good):
                raise forms.ValidationError("For 1000m run: Minimum should be the slowest, Very good the fastest (e.g. 06:00 >= 05:00 >= 04:20).")

            inst.minimum = minimum
            inst.good = good
            inst.very_good = very_good

        else:
            try:
                minimum = int(min_v)
                good = int(good_v)
                very_good = int(vg_v)
            except Exception:
                raise forms.ValidationError("For repetitions, enter whole numbers.")

            if minimum < 0 or good < 0 or very_good < 0:
                raise forms.ValidationError("Values must be positive.")

            if not (minimum <= good <= very_good):
                raise forms.ValidationError("For repetitions: Minimum ≤ Good ≤ Very good.")

            inst.minimum = minimum
            inst.good = good
            inst.very_good = very_good

        return cleaned

    def save(self, commit=True):
        if commit:
            self.instance.save()
        return self.instance
