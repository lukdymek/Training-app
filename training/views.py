
from django.shortcuts import render, get_object_or_404
from .models import Training, Participation
from django.shortcuts import redirect
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.utils.timezone import localtime
from .models import Training
from django.utils.dateparse import parse_datetime
from .forms import TrainingForm
from django.db.models import Count, Q
from django.utils.timezone import localtime
from .models import Person, TrainerSkill, Subject, Participation
import csv
from django.http import HttpResponse
from django.db.models import Sum, Q
from django.utils.timezone import localtime
from datetime import datetime
from decimal import Decimal, InvalidOperation
from django.views.decorators.http import require_POST
from django.conf import settings
from django.db import transaction, IntegrityError
from psycopg.types.range import Range
from .forms import TrainingForm
from django.views.decorators.http import require_GET
from training.constants import SYSPER_LABEL


def training_list(request):
    trainings = Training.objects.order_by("start_at")
    return render(request, "training/training_list.html", {"trainings": trainings})


def training_detail(request, pk):
    training = get_object_or_404(Training, pk=pk)

    trainees = Participation.objects.filter(
        training=training, role="TRAINEE"
    ).select_related("person")

    trainers = Participation.objects.filter(
        training=training, role="TRAINER"
    ).select_related("person")

    return render(
        request,
        "training/training_detail.html",
        {
            "training": training,
            "trainees": trainees,
            "trainers": trainers,
        },
    )


def add_trainee(request, pk):
    training = get_object_or_404(Training, pk=pk)

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

        # Capacity check
        trainee_count = Participation.objects.filter(
            training=training, role="TRAINEE"
        ).count()

        if trainee_count >= training.capacity:
            messages.error(request, "Training is already full.")
            return redirect("training_detail", pk=pk)

        # Duplicate check
        if Participation.objects.filter(
            training=training, person=person, role="TRAINEE"
        ).exists():
            messages.error(request, "This person is already a trainee.")
            return redirect("training_detail", pk=pk)

        # Create participation (overlap check runs automatically)
        try:
            Participation.objects.create(
                training=training,
                person=person,
                role="TRAINEE",
            )
            messages.success(request, f"{person} added as trainee.")
        except ValidationError as e:
            # Extract clean message (no '__all__')
            msg = e.messages[0] if e.messages else "Validation error."
            messages.error(request, msg)

    return redirect("training_detail", pk=pk)

def add_trainer(request, pk):
    training = get_object_or_404(Training, pk=pk)

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

        # If training has a subject, trainer must have that skill
        if training.subject_id:
            has_skill = TrainerSkill.objects.filter(trainer=person, subject=training.subject).exists()
            if not has_skill:
                messages.error(request, f"{person} is not approved to teach: {training.subject}")
                return redirect("training_detail", pk=pk)

        # Duplicate check
        if Participation.objects.filter(training=training, person=person, role="TRAINER").exists():
            messages.error(request, "This person is already a trainer for this training.")
            return redirect("training_detail", pk=pk)

        try:
            Participation.objects.create(training=training, person=person, role="TRAINER")
            messages.success(request, f"{person} added as trainer.")
        except ValidationError as e:
            messages.error(request, e.messages[0] if e.messages else "Validation error.")

    return redirect("training_detail", pk=pk)


def calendar_view(request):
    return render(request, "training/calendar.html")

def calendar_filters(request):
    subjects = list(Subject.objects.order_by("name").values("id", "name"))
    locations = list(
        Training.objects.order_by("location")
        .values_list("location", flat=True)
        .distinct()
    )
    locations = [l for l in locations if l]  # remove blanks
    return JsonResponse({"subjects": subjects, "locations": locations})

def trainings_json(request):
    subject_id = request.GET.get("subject_id", "").strip()
    location = request.GET.get("location", "").strip()

    trainings = Training.objects.all()

    if subject_id.isdigit():
        trainings = trainings.filter(subject_id=int(subject_id))

    if location:
        trainings = trainings.filter(location__iexact=location)

    # annotate with trainee count
    trainings = trainings.annotate(
        trainee_count=Count("participation", filter=Q(participation__role="TRAINEE"))
    ).select_related("subject").order_by("start_at")

    events = []
    for t in trainings:
        subject_name = t.subject.name if t.subject_id else "—"
        used = t.trainee_count
        cap = t.capacity

        events.append({
            "id": t.id,
            "title": t.course_name,
            "start": localtime(t.start_at).isoformat(),
            "end": localtime(t.end_at).isoformat(),
            "url": f"/trainings/{t.id}/",
            "extendedProps": {
                "subject": subject_name,
                "location": t.location,
                "capacity_used": used,
                "capacity_total": cap,
            }
        })

    return JsonResponse(events, safe=False)


def training_create(request):
    initial = {}

    # If calendar sends ?start=2026-01-13
    start = request.GET.get("start")
    if start:
        # Pre-fill start_at and end_at (same day, +1 hour)
        # For datetime-local input, we want YYYY-MM-DDTHH:MM
        initial["start_at"] = f"{start}T08:00"
        initial["end_at"] = f"{start}T09:00"

    if request.method == "POST":
        form = TrainingForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("calendar")
    else:
        form = TrainingForm(initial=initial)

    return render(request, "training/training_form.html", {"form": form})

def training_edit(request, pk):
    training = get_object_or_404(Training, pk=pk)

    if request.method == "POST":
        form = TrainingForm(request.POST, instance=training)
        if form.is_valid():
            try:
                with transaction.atomic():
                    t = form.save()

                    # Keep Participation.timespan in sync with updated training dates
                    new_range = Range(t.start_at, t.end_at, bounds='[)')
                    Participation.objects.filter(training=t).update(timespan=new_range)

                messages.success(request, "Training updated.")
                return redirect("training_detail", pk=pk)

            except IntegrityError:
                # In case something slipped through (race condition)
                messages.error(request, "Change not allowed: it would create an overlap for an assigned person.")
                return redirect("training_edit", pk=pk)
    else:
        form = TrainingForm(instance=training)

    return render(request, "training/training_form.html", {"form": form, "mode": "edit", "training": training})


def training_delete(request, pk):
    training = get_object_or_404(Training, pk=pk)

    if request.method == "POST":
        with transaction.atomic():
            # This will delete participations too if FK is CASCADE on Participation.training
            training.delete()
        messages.success(request, "Training deleted.")
        return redirect("calendar")

    return render(request, "training/training_confirm_delete.html", {"training": training})



def trainer_list(request):
    q = request.GET.get("q", "").strip()

    # ✅ Show nothing until user searches
    if not q:
        return render(request, "training/trainer_list.html", {
            "trainers": [],
            "q": q,
            "searched": False,
        })

    trainers = Person.objects.all()

    if q.isdigit():
        trainers = trainers.filter(sysper_id=int(q))
    else:
        trainers = trainers.filter(
            Q(last_name__icontains=q) |
            Q(first_name__icontains=q) |
            Q(email__icontains=q)
        )

    trainers = trainers.order_by("last_name", "first_name")[:200]
    return render(request, "training/trainer_list.html", {
        "trainers": trainers,
        "q": q,
        "searched": True,
    })


def trainer_detail(request, pk):
    trainer = get_object_or_404(Person, pk=pk)

    skills = TrainerSkill.objects.filter(trainer=trainer).select_related("subject").order_by("subject__name")
    all_subjects = Subject.objects.order_by("name")

    # trainings where this person is assigned as TRAINER
    trainings_as_trainer = Participation.objects.filter(
        person=trainer, role="TRAINER"
    ).select_related("training").order_by("-training__start_at")[:50]

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


def add_trainer_skill(request, pk):
    trainer = get_object_or_404(Person, pk=pk)

    if request.method == "POST":
        subject_id = request.POST.get("subject_id")
        if subject_id and subject_id.isdigit():
            subject = get_object_or_404(Subject, pk=int(subject_id))
            TrainerSkill.objects.get_or_create(trainer=trainer, subject=subject)

    return redirect("trainer_detail", pk=pk)


def remove_trainer_skill(request, pk, subject_id):
    trainer = get_object_or_404(Person, pk=pk)
    TrainerSkill.objects.filter(trainer=trainer, subject_id=subject_id).delete()
    return redirect("trainer_detail", pk=pk)


def reports_home(request):
    return render(request, "training/reports_home.html")


def report_person(request):
    sysper = request.GET.get("sysper_id", "").strip()
    person = None
    rows = []

    if sysper.isdigit():
        person = Person.objects.filter(sysper_id=int(sysper)).first()
        if person:
            rows = (Participation.objects
                    .filter(person=person)
                    .select_related("training", "training__subject")
                    .order_by("-training__start_at"))

    return render(request, "training/report_person.html", {
        "sysper": sysper,
        "person": person,
        "rows": rows,
    })


def report_person_export(request):
    sysper = request.GET.get("sysper_id", "").strip()
    if not sysper.isdigit():
        return HttpResponse("Invalid sysper_id", status=400)

    person = Person.objects.filter(sysper_id=int(sysper)).first()
    if not person:
        return HttpResponse("Person not found", status=404)

    rows = (Participation.objects
            .filter(person=person)
            .select_related("training", "training__subject")
            .order_by("-training__start_at"))

    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="person_{person.sysper_id}_history.csv"'
    w = csv.writer(resp)

    w.writerow([SYSPER_LABEL, "Last name", "First name", "Role", "Course", "Subject", "Start", "End", "Location"])
    for p in rows:
        t = p.training
        w.writerow([
            person.sysper_id,
            person.last_name,
            person.first_name,
            p.role,
            t.course_name,
            (t.subject.name if t.subject_id else ""),
            localtime(t.start_at).strftime("%Y-%m-%d %H:%M"),
            localtime(t.end_at).strftime("%Y-%m-%d %H:%M"),
            t.location,
        ])
    return resp


def report_trainers(request):
    """
    Totals trainer days per trainer for a given month.
    If days is empty, totals will be 0 (we can improve later).
    """
    year = request.GET.get("year", "").strip()
    month = request.GET.get("month", "").strip()

    now = datetime.now()
    y = int(year) if year.isdigit() else now.year
    m = int(month) if month.isdigit() else now.month

    start = datetime(y, m, 1)
    # next month boundary
    if m == 12:
        end = datetime(y + 1, 1, 1)
    else:
        end = datetime(y, m + 1, 1)

    qs = (Participation.objects
          .filter(role="TRAINER", training__start_at__gte=start, training__start_at__lt=end)
          .select_related("person")
          .values("person__id", "person__sysper_id", "person__last_name", "person__first_name")
          .annotate(total_days=Sum("days"))
          .order_by("person__last_name", "person__first_name"))

    return render(request, "training/report_trainers.html", {
        "year": y,
        "month": m,
        "rows": qs,
    })


def report_trainers_export(request):
    year = request.GET.get("year", "").strip()
    month = request.GET.get("month", "").strip()

    now = datetime.now()
    y = int(year) if year.isdigit() else now.year
    m = int(month) if month.isdigit() else now.month

    start = datetime(y, m, 1)
    end = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)

    qs = (Participation.objects
          .filter(role="TRAINER", training__start_at__gte=start, training__start_at__lt=end)
          .select_related("person")
          .values("person__sysper_id", "person__last_name", "person__first_name")
          .annotate(total_days=Sum("days"))
          .order_by("person__last_name", "person__first_name"))

    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="trainer_days_{y:04d}_{m:02d}.csv"'
    w = csv.writer(resp)
    w.writerow([SYSPER_LABEL, "Last name", "First name", "Total trainer days (month)"])

    for r in qs:
        w.writerow([
            r["person__sysper_id"],
            r["person__last_name"],
            r["person__first_name"],
            float(r["total_days"] or 0),
        ])

    return resp

@require_POST
def update_trainer_days(request, pk, participation_id):
    training = get_object_or_404(Training, pk=pk)

    # Make sure we only edit TRAINER participations for THIS training
    p = get_object_or_404(
        Participation,
        pk=participation_id,
        training=training,
        role="TRAINER",
    )

    raw = (request.POST.get("days") or "").strip().replace(",", ".")
    if raw == "":
        # allow clearing
        p.days = None
        p.save(update_fields=["days"])
        messages.success(request, f"Days cleared for {p.person}.")
        return redirect("training_detail", pk=pk)

    try:
        days = Decimal(raw)
    except InvalidOperation:
        messages.error(request, "Days must be a number (e.g. 1 or 2.5).")
        return redirect("training_detail", pk=pk)

    if days < 0:
        messages.error(request, "Days cannot be negative.")
        return redirect("training_detail", pk=pk)

    p.days = days
    p.save(update_fields=["days"])
    messages.success(request, f"Days updated for {p.person}: {days}")
    return redirect("training_detail", pk=pk)

@require_GET
def people_search(request, pk):
    training = get_object_or_404(Training, pk=pk)

    role = request.GET.get("role", "TRAINEE").upper().strip()
    q = request.GET.get("q", "").strip()

    if role not in {"TRAINEE", "TRAINER"}:
        return JsonResponse({"results": []})

    # keep it efficient
    if len(q) < 2:
        return JsonResponse({"results": []})

    people = Person.objects.all()

    # search by name + (optionally) sysper prefix
    if q.isdigit():
        people = people.filter(
            Q(sysper_id__startswith=int(q)) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q)
        )
    else:
        people = people.filter(
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q)
        )

    # exclude already added for this role
    existing_person_ids = Participation.objects.filter(
        training=training, role=role
    ).values_list("person_id", flat=True)
    people = people.exclude(id__in=existing_person_ids)

    # if adding TRAINER and training has subject -> only show approved trainers
    if role == "TRAINER" and training.subject_id:
        people = people.filter(trainerskill__subject=training.subject).distinct()

    people = people.order_by("last_name", "first_name")[:20]

    return JsonResponse({
        "results": [
            {
                "sysper_id": p.sysper_id,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "label": f"{p.last_name} {p.first_name} ({p.sysper_id})",
            }
            for p in people
        ]
    })