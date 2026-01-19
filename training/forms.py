from django import forms
from .models import Training
from django.core.exceptions import ValidationError
from django.db.models import Q
from psycopg.types.range import Range
from .models import Training, Participation
from .models import Person


class TrainingForm(forms.ModelForm):
    class Meta:
        model = Training
        fields = ["course_name", "subject", "start_at", "end_at", "location", "capacity", "remarks"]
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

        # If editing an existing training, block date changes that would create overlaps
        if self.instance and self.instance.pk and start and end:
            new_range = Range(start, end, bounds='[)')

            # People currently assigned to THIS training (trainees + trainers)
            person_ids = list(
                Participation.objects.filter(training=self.instance)
                .values_list("person_id", flat=True)
                .distinct()
            )

            if person_ids:
                # Any other participation for those people that overlaps the new range?
                conflicts = (
                    Participation.objects.filter(person_id__in=person_ids)
                    .exclude(training=self.instance)
                    .filter(timespan__overlap=new_range)
                    .select_related("person", "training")
                    .order_by("person_id", "timespan")
                )

                if conflicts.exists():
                    # Show a readable message (first conflict only to keep it simple)
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
