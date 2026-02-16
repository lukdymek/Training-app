from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from training.constants import SYSPER_LABEL
from training.forms import AddMultipleTrainersForm
from training.models import Participation, Person, Subject, TrainerSkill, Training

from .common import staff_required


@login_required
@staff_required
def add_trainer(request, pk):
    training = get_object_or_404(Training, pk=pk)
    total_days = training_total_days(training)

    if request.method == "POST":
        sysper_id = request.POST.get("sysper_id", "").strip()

        if not sysper_id.isdigit():
            messages.error(request, f"Invalid {SYSPER_LABEL}.")
            return redirect("training_detail", pk=pk)

        try:
            person = Person.objects.get(sysper_id=int(sysper_id))
        except Person.DoesNotExist:
            messages.error(request, "Person not found.")
            return redirect("training_detail", pk=pk)

        if training.subject_id:
            has_skill = TrainerSkill.objects.filter(trainer=person, subject=training.subject).exists()
            if not has_skill:
                messages.error(request, f"{person} is not approved to teach: {training.subject}")
                return redirect("training_detail", pk=pk)

        old = Participation.objects.filter(
            training=training,
            person=person,
            role="TRAINER",
            removed_at__isnull=False,
        ).order_by("-removed_at").first()

        if old:
            old.removed_at = None
            old.removed_by = None
            old.removed_reason = ""
            old.status = "PENDING"
            old.status_changed_at = None
            old.status_changed_by = None
            old.days = None
            if old.days is None and total_days > 0:
                old.days = total_days

            old.save(
                update_fields=[
                    "removed_at",
                    "removed_by",
                    "removed_reason",
                    "status",
                    "status_changed_at",
                    "status_changed_by",
                ]
            )
            messages.success(request, f"{person} re-added as trainer.")
            return redirect("training_detail", pk=pk)

        if Participation.objects.filter(
            training=training,
            person=person,
            role="TRAINER",
            removed_at__isnull=True,
        ).exists():
            messages.error(request, "This person is already a trainer for this training.")
            return redirect("training_detail", pk=pk)

        try:
            Participation.objects.create(
                training=training,
                person=person,
                role="TRAINER",
                days=(total_days if total_days > 0 else None),
            )
            messages.success(request, f"{person} added as trainer.")
        except ValidationError as e:
            messages.error(request, e.messages[0] if e.messages else "Validation error.")
        except IntegrityError:
            messages.error(request, "Could not add trainer (already exists).")

    return redirect("training_detail", pk=pk)


@login_required
@staff_required
def trainer_list(request):
    q = request.GET.get("q", "").strip()

    if not q:
        return render(
            request,
            "training/trainer_list.html",
            {
                "trainers": [],
                "q": q,
                "searched": False,
            },
        )

    trainers = Person.objects.all()

    if q.isdigit():
        trainers = trainers.filter(sysper_id=int(q))
    else:
        trainers = trainers.filter(
            Q(last_name__icontains=q) | Q(first_name__icontains=q) | Q(email__icontains=q)
        )

    trainers = trainers.order_by("last_name", "first_name")[:200]
    return render(
        request,
        "training/trainer_list.html",
        {
            "trainers": trainers,
            "q": q,
            "searched": True,
        },
    )


@login_required
@staff_required
def trainer_detail(request, pk):
    trainer = get_object_or_404(Person, pk=pk)

    skills = TrainerSkill.objects.filter(trainer=trainer).select_related("subject").order_by("subject__name")
    all_subjects = Subject.objects.order_by("name")

    trainings_as_trainer = Participation.objects.filter(person=trainer, role="TRAINER").select_related(
        "training"
    ).order_by("-training__start_at")[:50]

    return render(
        request,
        "training/trainer_detail.html",
        {
            "trainer": trainer,
            "skills": skills,
            "all_subjects": all_subjects,
            "trainings_as_trainer": trainings_as_trainer,
        },
    )


@login_required
@staff_required
def add_trainer_skill(request, pk):
    trainer = get_object_or_404(Person, pk=pk)

    if request.method == "POST":
        subject_id = request.POST.get("subject_id")
        if subject_id and subject_id.isdigit():
            subject = get_object_or_404(Subject, pk=int(subject_id))
            TrainerSkill.objects.get_or_create(trainer=trainer, subject=subject)

    return redirect("trainer_detail", pk=pk)


@login_required
@staff_required
@require_POST
def remove_trainer_skill(request, pk, subject_id):
    trainer = get_object_or_404(Person, pk=pk)
    subject = get_object_or_404(Subject, pk=subject_id)

    deleted, _ = TrainerSkill.objects.filter(trainer=trainer, subject=subject).delete()
    if deleted:
        messages.success(request, f"Removed skill: {subject.name}")
    else:
        messages.info(request, "Skill not found (nothing removed).")

    return redirect("trainer_detail", pk=trainer.pk)


@login_required
@staff_required
@require_POST
def update_trainer_days(request, pk, participation_id):
    training = get_object_or_404(Training, pk=pk)
    max_days = training_total_days(training)

    participation = get_object_or_404(
        Participation,
        pk=participation_id,
        training=training,
        role="TRAINER",
        removed_at__isnull=True,
    )

    raw = (request.POST.get("days") or "").strip().replace(",", ".")
    if raw == "":
        participation.days = None
        participation.save(update_fields=["days"])
        messages.success(request, f"Days cleared for {participation.person}.")
        return redirect("training_detail", pk=pk)

    try:
        days = Decimal(raw)
    except InvalidOperation:
        messages.error(request, "Days must be a number (e.g. 1 or 2.5).")
        return redirect("training_detail", pk=pk)

    if days < 0:
        messages.error(request, "Days cannot be negative.")
        return redirect("training_detail", pk=pk)

    if max_days > 0 and days > max_days:
        messages.error(request, f"Days cannot be greater than the training duration ({max_days} days).")
        return redirect("training_detail", pk=pk)

    participation.days = days
    participation.save(update_fields=["days"])
    messages.success(request, f"Days updated for {participation.person}: {days}")
    return redirect("training_detail", pk=pk)


def training_total_days(training) -> Decimal:
    start = training.start_at
    end = training.end_at

    if hasattr(start, "date"):
        start = start.date()
    if hasattr(end, "date"):
        end = end.date()

    if not isinstance(start, date) or not isinstance(end, date) or end < start:
        return Decimal("0")

    days = (end - start).days + 1
    return Decimal(str(days))


@login_required
@staff_required
@require_POST
def add_trainers_bulk(request, pk):
    training = get_object_or_404(Training, pk=pk)
    total_days = training_total_days(training)

    start = training.start_at
    end = training.end_at

    available_qs = Person.objects.all()

    if training.subject_id:
        available_qs = available_qs.filter(trainerskill__subject=training.subject).distinct()
    else:
        available_qs = available_qs.none()

    existing_people = Participation.objects.filter(
        training=training,
        removed_at__isnull=True,
    ).values_list("person_id", flat=True)
    available_qs = available_qs.exclude(id__in=existing_people)

    conflicting_people = Participation.objects.filter(
        removed_at__isnull=True,
        training__start_at__lt=end,
        training__end_at__gt=start,
    ).values_list("person_id", flat=True).distinct()
    available_qs = available_qs.exclude(id__in=conflicting_people)

    form = AddMultipleTrainersForm(request.POST, queryset=available_qs)
    if not form.is_valid():
        messages.error(request, "Please select trainers from the available list.")
        return redirect("training_detail", pk=pk)

    selected = list(form.cleaned_data["trainers"])
    added = 0
    reactivated = 0
    skipped = 0
    overlap_messages = []

    for person in selected:
        participation = Participation.objects.filter(
            training=training,
            person=person,
            role="TRAINER",
            removed_at__isnull=False,
        ).order_by("-removed_at").first()

        if participation:
            participation.removed_at = None
            participation.removed_by = None
            participation.removed_reason = ""
            participation.status = "PENDING"
            participation.status_changed_at = None
            participation.status_changed_by = None
            participation.days = None
            if participation.days is None and total_days > 0:
                participation.days = total_days

            participation.save(
                update_fields=[
                    "removed_at",
                    "removed_by",
                    "removed_reason",
                    "status",
                    "status_changed_at",
                    "status_changed_by",
                ]
            )
            reactivated += 1
            continue

        if Participation.objects.filter(
            training=training,
            person=person,
            removed_at__isnull=True,
        ).exists():
            skipped += 1
            overlap_messages.append(f"{person}: already participating in this training.")
            continue

        try:
            Participation.objects.create(
                training=training,
                person=person,
                role="TRAINER",
                days=(total_days if total_days > 0 else None),
            )
            added += 1
        except ValidationError as e:
            skipped += 1

            msg_list = e.messages or []
            if hasattr(e, "message_dict") and "__all__" in e.message_dict:
                msg_list = e.message_dict["__all__"]

            if msg_list:
                overlap_messages.append(f"{person}: {msg_list[0]}")
            else:
                overlap_messages.append(f"{person}: cannot be added due to a validation rule.")
        except IntegrityError:
            skipped += 1
            overlap_messages.append(f"{person}: already added or blocked by a database constraint.")

    if added:
        messages.success(request, f"Added {added} trainer(s).")
    if reactivated:
        messages.success(request, f"Re-activated {reactivated} previously removed trainer(s).")
    if skipped:
        messages.warning(request, f"Skipped {skipped} trainer(s).")

    for line in overlap_messages[:3]:
        messages.warning(request, line)
    if len(overlap_messages) > 3:
        messages.warning(request, f"...and {len(overlap_messages) - 3} more.")

    return redirect("training_detail", pk=pk)


__all__ = [
    "add_trainer",
    "add_trainer_skill",
    "add_trainers_bulk",
    "remove_trainer_skill",
    "trainer_detail",
    "trainer_list",
    "training_total_days",
    "update_trainer_days",
]
