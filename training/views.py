from django.shortcuts import render, get_object_or_404
from django.shortcuts import redirect
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.utils.timezone import localtime
from django.utils.dateparse import parse_datetime
from .forms import TrainingForm
from django.db.models import Count, Q
from django.utils.timezone import localtime
from .models import Person, TrainerSkill, Subject, Participation, Training, EmailVerification
import csv
from django.http import HttpResponse
from django.db.models import Sum
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
from django.shortcuts import get_object_or_404, redirect
from .forms import AddMultipleTrainersForm
from django.db.models import Prefetch
from django.utils.timezone import now
from django.core.paginator import Paginator
from django.utils.http import urlencode
from django.utils import timezone
from datetime import timedelta
from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.contrib.auth import authenticate, login, get_user_model
from django.http import HttpResponseForbidden
from functools import wraps
from django.core.exceptions import PermissionDenied
import random
from django.contrib.auth.hashers import make_password
from django.core.mail import send_mail




CODE_TTL_MINUTES = 15
RESEND_COOLDOWN_SECONDS = 60


def staff_required(view_func):
    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped

def login_view(request):
    if request.user.is_authenticated:
        return redirect("calendar")

    if request.method == "POST":
        identifier = (request.POST.get("identifier") or "").strip()
        password = request.POST.get("password") or ""
        next_url = request.POST.get("next") or request.GET.get("next") or ""

        # ✅ Only allow email OR exactly "admin"
        is_admin_login = (identifier.lower() == "admin")
        looks_like_email = ("@" in identifier and "." in identifier)

        if not is_admin_login and not looks_like_email:
            messages.error(request, "Please enter a valid email address.")
            return render(request, "training/login.html", {"next": next_url})

        user = None

        # 1) Admin login by username "admin"
        if is_admin_login:
            user = authenticate(request, username="admin", password=password)

        # 2) Email login (find user by email, then auth using username)
        if user is None and looks_like_email:
            User = get_user_model()
            u = User.objects.filter(email__iexact=identifier).first()
            if u:
                user = authenticate(request, username=u.username, password=password)

        if user is not None:
            login(request, user)
            return redirect(next_url or "calendar")

        messages.error(request, "Invalid login details. Please try again.")

    return render(request, "training/login.html", {"next": request.GET.get("next", "")})

@login_required
@staff_required
def training_list(request):
    today = timezone.localdate()

    qs = (
        Training.objects.all()
        .annotate(
            trainee_count=Count(
                "participation",
                filter=Q(participation__role="TRAINEE"),
                distinct=True,
            ),
            trainer_count=Count(
                "participation",
                filter=Q(participation__role="TRAINER"),
                distinct=True,
            ),
        )
        .order_by("-start_at")
    )

    current_trainings = qs.filter(start_at__date__lte=today, end_at__date__gte=today).order_by("start_at")
    future_trainings = qs.filter(start_at__date__gt=today).order_by("start_at")
    completed_trainings = qs.filter(end_at__date__lt=today).order_by("-start_at")

    return render(
        request,
        "training/training_list.html",
        {
            "current_trainings": current_trainings,
            "future_trainings": future_trainings,
            "completed_trainings": completed_trainings,
            "today": today,
        },
    )

@login_required
@staff_required
def training_detail(request, pk):
    training = get_object_or_404(Training, pk=pk)

    # total training duration in days (inclusive)
    # Example: Jan 1 to Jan 4 => 4 days
    duration_days = (training.end_at.date() - training.start_at.date()).days + 1
    if duration_days < 1:
        duration_days = 1

    trainees = Participation.objects.filter(
        training=training, role="TRAINEE"
    ).select_related("person")

    trainers = Participation.objects.filter(
        training=training, role="TRAINER"
    ).select_related("person")

    start = training.start_at
    end = training.end_at

    # Build "available trainers" list
    if training.subject_id:
        available_trainers = Person.objects.filter(
            trainerskill__subject=training.subject
        ).distinct()
    else:
        available_trainers = Person.objects.none()

    # exclude trainers already in this training
    existing_trainers = Participation.objects.filter(
        training=training, role="TRAINER"
    ).values_list("person_id", flat=True)

    available_trainers = available_trainers.exclude(id__in=existing_trainers)

    # exclude anyone busy in overlapping training (any role)
    conflicting_people = Participation.objects.filter(
        training__start_at__lt=end,
        training__end_at__gt=start,
    ).values_list("person_id", flat=True).distinct()

    available_trainers = available_trainers.exclude(id__in=conflicting_people)

    bulk_trainers_form = AddMultipleTrainersForm(queryset=available_trainers)

    context = {
        "training": training,
        "trainees": trainees,
        "trainers": trainers,
        "available_trainers": available_trainers,
        "bulk_trainers_form": bulk_trainers_form,
        "duration_days": duration_days, 
    }

    return render(request, "training/training_detail.html", context)


@login_required
@staff_required
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

@login_required
@staff_required
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

@login_required
def calendar_view(request):
    return render(request, "training/calendar.html")

@login_required
def calendar_filters(request):
    subjects = list(Subject.objects.order_by("name").values("id", "name"))
    locations = list(
        Training.objects.order_by("location")
        .values_list("location", flat=True)
        .distinct()
    )
    locations = [l for l in locations if l]  # remove blanks
    return JsonResponse({"subjects": subjects, "locations": locations})

@login_required
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

@login_required
@staff_required
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

    return render(request, "training/training_form.html", {
        "form": form,
        "page_title": "Create training",
        "submit_label": "Create",
    })

@login_required
@staff_required
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

    return render(request, "training/training_form.html", {
        "form": form,
        "training": training,
        "page_title": "Edit training",
        "submit_label": "Save changes",
        "is_edit": True,
    })


@login_required
@staff_required
def training_delete(request, pk):
    training = get_object_or_404(Training, pk=pk)

    if request.method == "POST":
        with transaction.atomic():
            # This will delete participations too if FK is CASCADE on Participation.training
            training.delete()
        messages.success(request, "Training deleted.")
        return redirect("calendar")

    return render(request, "training/training_confirm_delete.html", {"training": training})


@login_required
@staff_required
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

@login_required
@staff_required
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
def reports_home(request):
    return render(request, "training/reports_home.html")

@login_required
@staff_required
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

@login_required
@staff_required
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

@login_required
@staff_required
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

@login_required
@staff_required
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

@login_required
@staff_required
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

@login_required
@staff_required
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

@login_required
@staff_required
@require_POST
def remove_trainee(request, pk, person_id):
    training = get_object_or_404(Training, pk=pk)
    Participation.objects.filter(
        training=training,
        person_id=person_id,
        role="TRAINEE",
    ).delete()
    messages.success(request, "Trainee removed from training.")
    return redirect("training_detail", pk=pk)

@login_required
@staff_required
@require_POST
def remove_trainer(request, pk, person_id):
    training = get_object_or_404(Training, pk=pk)
    Participation.objects.filter(
        training=training,
        person_id=person_id,
        role="TRAINER",
    ).delete()
    messages.success(request, "Trainer removed from training.")
    return redirect("training_detail", pk=pk)


@login_required
@staff_required
@require_POST
def remove_participation(request, pk, participation_id):
    training = get_object_or_404(Training, pk=pk)
    participation = get_object_or_404(
        Participation,
        pk=participation_id,
        training=training,
    )
    participation.delete()
    messages.success(request, "Removed from training.")
    return redirect("training_detail", pk=pk)

@login_required
@staff_required
@require_POST
def add_trainers_bulk(request, pk):
    training = get_object_or_404(Training, pk=pk)

    # Build the same available queryset used on the page (see section 3)
    start = training.start_at
    end = training.end_at

    available_qs = Person.objects.all()

    if training.subject_id:
        available_qs = available_qs.filter(trainerskill__subject=training.subject).distinct()
    else:
        available_qs = available_qs.none()

    # exclude people already in this training as TRAINER
    existing_trainers = Participation.objects.filter(training=training, role="TRAINER").values_list("person_id", flat=True)
    available_qs = available_qs.exclude(id__in=existing_trainers)

    # exclude people who are participating in ANY training that overlaps
    conflicting_people = Participation.objects.filter(
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
    skipped = 0

    for person in selected:
        try:
            Participation.objects.create(training=training, person=person, role="TRAINER")
            added += 1
        except IntegrityError:
            # already exists or overlaps constraint hit
            skipped += 1

    if added:
        messages.success(request, f"Added {added} trainer(s).")
    if skipped:
        messages.warning(request, f"Skipped {skipped} trainer(s) (already added or conflicting).")

    return redirect("training_detail", pk=pk)

@login_required
def person_history(request, person_id):
    person = get_object_or_404(Person, pk=person_id)
    t = now()

    base_qs = (
        Participation.objects
        .filter(person=person)
        .select_related("training", "training__subject")
    )

    completed = base_qs.filter(training__end_at__lt=t).order_by("-training__end_at", "-training__start_at")
    upcoming = base_qs.filter(training__start_at__gte=t).order_by("training__start_at")

    # Optional: include "ongoing" trainings (started but not ended yet)
    ongoing = base_qs.filter(training__start_at__lt=t, training__end_at__gte=t).order_by("training__end_at")

    return render(
        request,
        "training/person_history.html",
        {
            "person": person,
            "completed": completed,
            "upcoming": upcoming,
            "ongoing": ongoing,
        },
    )

@login_required
@staff_required
def people_list(request):
    q = (request.GET.get("q") or "").strip()

    people = Person.objects.all().order_by("last_name", "first_name")

    if q:
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

    paginator = Paginator(people, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "training/people_list.html",
        {
            "q": q,
            "page_obj": page_obj,
        },
    )

@login_required
@staff_required
@require_GET
def people_search_api(request):
    q = (request.GET.get("q") or "").strip()

    if len(q) < 2:
        return JsonResponse({"results": []})

    qs = Person.objects.filter(
        Q(sysper_id__icontains=q) |
        Q(last_name__icontains=q) |
        Q(first_name__icontains=q) |
        Q(email__icontains=q)
    ).order_by("last_name", "first_name")[:25]

    results = []
    
    for p in qs:
        results.append({
            "id": p.id,
            "sysper_id": p.sysper_id,
            "first_name": p.first_name,
            "last_name": p.last_name,
            # Display label used by dropdowns
            "label": f"{p.sysper_id} — {p.last_name.upper()} {p.first_name}",
        })

    return JsonResponse({"results": results})

@login_required
@staff_required
@require_GET
def trainer_search_api(request):
    q = (request.GET.get("q") or "").strip()

    # Keep it consistent with People page: don’t search until 2+ chars
    if len(q) < 2:
        return JsonResponse({"results": []})

    # “Who is a trainer?”
    # - has TrainerSkill OR
    # - has ever been assigned as TRAINER in Participation
    base_qs = Person.objects.filter(
        Q(trainerskill__isnull=False) | Q(participation__role="TRAINER")
    ).distinct()

    qs = base_qs.filter(
        Q(sysper_id__icontains=q) |
        Q(last_name__icontains=q) |
        Q(first_name__icontains=q) |
        Q(email__icontains=q)
    ).order_by("last_name", "first_name")[:25]

    results = []
    for p in qs:
        results.append({
            "id": p.id,
            "sysper_id": p.sysper_id,
            "first_name": p.first_name,
            "last_name": p.last_name,
            "email": p.email,
            "label": f"{p.sysper_id} — {p.last_name.upper()} {p.first_name}",
        })

    return JsonResponse({"results": results})

@login_required
@staff_required
@login_required
def recurring_training(request):
    """
    Page with dropdowns (subject + filter) and a dynamic list loaded via API.
    """
    subjects = Subject.objects.filter(is_recurring=True).order_by("name")
    return render(request, "training/recurring_training.html", {"subjects": subjects})


@staff_required
@require_GET
@login_required
def recurring_training_api(request):
    """
    Returns JSON list of people with:
      - last completed date for selected subject
      - expiry date (last + validity days)
      - days remaining
      - status: ok / expiring / expired
    Query params:
      - subject_id (required)
      - window: all | 90 | 30 | expired   (default: all)
      - validity_days: int (default: 365)  (optional, but useful later)
    """
    subject_id = request.GET.get("subject_id")
    window = (request.GET.get("window") or "all").strip().lower()

    if not subject_id:
        return JsonResponse({"results": [], "error": "subject_id is required"}, status=400)

    try:
        subject_id = int(subject_id)
    except ValueError:
        return JsonResponse({"results": [], "error": "subject_id must be an integer"}, status=400)

    subject = Subject.objects.filter(id=subject_id, is_recurring=True).first()
    if not subject:
        return JsonResponse({"results": [], "error": "Subject not found or not recurring"}, status=404)

    validity_days = subject.validity_days
    today = timezone.localdate()

    # One row per person: find their latest completion for that subject
    rows = (
        Participation.objects
        .filter(
            role="TRAINEE",
            training__subject_id=subject_id,
            person__is_active=True,
        )
        .values(
            "person_id",
            "person__sysper_id",
            "person__first_name",
            "person__last_name",
            "person__email",
        )
        .annotate(last_end_at=Max("training__end_at"))
    )

    results = []
    for r in rows:
        last_end_at = r["last_end_at"]
        if not last_end_at:
            # Shouldn’t happen due to Max, but keep safe
            continue

        last_completed = timezone.localtime(last_end_at).date()
        expires_on = last_completed + timedelta(days=validity_days)
        days_remaining = (expires_on - today).days

        if days_remaining < 0:
            status = "expired"
        elif days_remaining <= 30:
            status = "expiring"
        else:
            status = "ok"

        # Apply window filter
        if window == "expired" and status != "expired":
            continue
        if window == "30" and not (0 <= days_remaining <= 30):
            continue
        if window == "90" and not (0 <= days_remaining <= 90):
            continue
        # window == "all" -> include everything

        results.append({
            "person_id": r["person_id"],
            "sysper_id": r["person__sysper_id"],
            "first_name": r["person__first_name"],
            "last_name": r["person__last_name"],
            "email": r["person__email"] or "",
            "last_completed": last_completed.isoformat(),
            "expires_on": expires_on.isoformat(),
            "days_remaining": days_remaining,
            "status": status,
        })

    # Sort: expired first (most overdue first), then expiring soon, then OK
    status_rank = {"expired": 0, "expiring": 1, "ok": 2}
    results.sort(key=lambda x: (status_rank.get(x["status"], 9), x["days_remaining"]))

    return JsonResponse({
        "results": results,
        "meta": {
            "today": today.isoformat(),
            "validity_days": validity_days,
            "window": window,
            "subject": subject.name,
        }
    })


@login_required
def my_history(request):
    email = (request.user.email or "").strip().lower()

    if not email:
        messages.error(request, "Your account has no email address set. Please contact admin.")
        return redirect("calendar")

    person = Person.objects.filter(email__iexact=email).first()
    if not person:
        messages.error(
            request,
            "No Person record found for your email. Please contact admin."
        )
        return redirect("calendar")

    # You already have this view + URL name in your project:
    return redirect("person_history", person_id=person.pk)

def custom_403(request, exception=None):
    return render(request, "403.html", status=403)

def _send_verification_code(email: str) -> str:
    code = f"{random.randint(0, 999999):06d}"
    try:
        send_mail(
            subject="Your Training App verification code",
            message=f"Your verification code is: {code}\n\nIt expires in 15 minutes.",
            from_email=None,
            recipient_list=[email],
            fail_silently=False,
        )
    except Exception:
        # Don’t crash the request / worker
        raise RuntimeError("Email sending failed. Please try again in a minute.")
    return code


def register_request(request):
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()

        if not email or "@" not in email:
            messages.error(request, "Please enter a valid email address.")
            return redirect("register")

        if not Person.objects.filter(email__iexact=email).exists():
            messages.error(request, "This email address is not recognized. Please contact your administrator.")
            return redirect("register")

        # Cooldown: do not allow sending again within 60s
        latest = EmailVerification.objects.filter(email=email).order_by("-created_at").first()
        if latest:
            delta = timezone.now() - latest.created_at
            if delta.total_seconds() < RESEND_COOLDOWN_SECONDS:
                wait = int(RESEND_COOLDOWN_SECONDS - delta.total_seconds())
                messages.error(request, f"Please wait {wait} seconds before requesting another code.")
                return redirect("register")

        # Invalidate previous unused codes for this email (keeps DB tidy)
        EmailVerification.objects.filter(email=email, used_at__isnull=True).update(used_at=timezone.now())

        try:
            code = _send_verification_code(email)
        except RuntimeError as e:
            messages.error(request, str(e))
            return redirect("register")
        
        EmailVerification.objects.create(email=email, code=code)

        request.session["reg_email"] = email
        request.session["reg_verified"] = False
        request.session["reg_last_sent_at"] = timezone.now().isoformat()

        messages.success(request, "Verification code sent. Please check your email.")
        return redirect("register_verify")

    return render(request, "training/register.html")


def register_verify(request):
    email = (request.session.get("reg_email") or "").strip().lower()
    if not email:
        return redirect("register")

    if request.method == "POST":
        code = (request.POST.get("code") or "").strip()

        ev = EmailVerification.objects.filter(email=email, used_at__isnull=True).order_by("-created_at").first()
        if not ev:
            messages.error(request, "No active verification code found. Please request a new code.")
            return redirect("register")

        if ev.is_expired():
            messages.error(request, "Verification code expired. Please request a new code.")
            return redirect("register")

        if code != ev.code:
            messages.error(request, "Invalid code. Please try again.")
            return redirect("register_verify")

        ev.mark_used()
        request.session["reg_verified"] = True
        messages.success(request, "Code verified. Please set your password.")
        return redirect("register_set_password")

    # Pass cooldown remaining to template (nice UX)
    remaining = 0
    last_sent = request.session.get("reg_last_sent_at")
    if last_sent:
        try:
            last_dt = timezone.datetime.fromisoformat(last_sent)
            if timezone.is_naive(last_dt):
                last_dt = timezone.make_aware(last_dt, timezone.get_current_timezone())
            remaining = max(0, RESEND_COOLDOWN_SECONDS - int((timezone.now() - last_dt).total_seconds()))
        except Exception:
            remaining = 0

    return render(request, "training/register_verify.html", {"email": email, "resend_remaining": remaining})


def register_resend(request):
    # Resend should be POST to avoid accidental triggers
    if request.method != "POST":
        return redirect("register_verify")

    email = (request.session.get("reg_email") or "").strip().lower()
    if not email:
        return redirect("register")

    # Cooldown check based on DB latest send
    latest = EmailVerification.objects.filter(email=email).order_by("-created_at").first()
    if latest:
        delta = timezone.now() - latest.created_at
        if delta.total_seconds() < RESEND_COOLDOWN_SECONDS:
            wait = int(RESEND_COOLDOWN_SECONDS - delta.total_seconds())
            messages.error(request, f"Please wait {wait} seconds before resending the code.")
            return redirect("register_verify")

    # Invalidate previous unused codes
    EmailVerification.objects.filter(email=email, used_at__isnull=True).update(used_at=timezone.now())

    code = _send_verification_code(email)
    EmailVerification.objects.create(email=email, code=code)

    request.session["reg_last_sent_at"] = timezone.now().isoformat()
    messages.success(request, "A new verification code has been sent.")
    return redirect("register_verify")


def register_set_password(request):
    email = (request.session.get("reg_email") or "").strip().lower()
    verified = request.session.get("reg_verified") is True

    if not email or not verified:
        return redirect("register")

    if request.method == "POST":
        pw1 = request.POST.get("password1") or ""
        pw2 = request.POST.get("password2") or ""

        if len(pw1) < 8:
            messages.error(request, "Password must be at least 8 characters.")
            return redirect("register_set_password")

        if pw1 != pw2:
            messages.error(request, "Passwords do not match.")
            return redirect("register_set_password")

        User = get_user_model()

        user = User.objects.filter(email__iexact=email).first()
        if not user:
            base = email.split("@")[0]
            username = base
            i = 1
            while User.objects.filter(username=username).exists():
                i += 1
                username = f"{base}{i}"
            user = User(username=username, email=email, is_active=True, is_staff=False, is_superuser=False)

        user.password = make_password(pw1)
        user.save()

        # Cleanup session
        for k in ("reg_email", "reg_verified", "reg_last_sent_at"):
            request.session.pop(k, None)

        messages.success(request, "Account created. You can now log in.")
        return redirect("login")

    return render(request, "training/register_set_password.html", {"email": email})

@staff_required
@login_required
def training_finder(request):
    """
    Show people who completed a selected training subject.
    Optional filters:
      - contingent (from Person.contingent)
      - category (from Person.category)
    Sorting:
      - sysper_id asc/desc
    """
    # Dropdown: training "type" (Subject)
    subjects = Subject.objects.order_by("name")

    # Read filters
    subject_id = request.GET.get("subject_id") or ""
    contingent = request.GET.get("contingent") or ""
    category = request.GET.get("category") or ""
    sort = (request.GET.get("sort") or "sysper_asc").strip().lower()

    # Build distinct options from Person table (safe even if some are blank)
    contingent_options = (
        Person.objects.exclude(contingent__isnull=True).exclude(contingent="")
        .values_list("contingent", flat=True).distinct().order_by("contingent")
    )
    category_options = (
        Person.objects.exclude(category__isnull=True).exclude(category="")
        .values_list("category", flat=True).distinct().order_by("category")
    )

    results = []
    selected_subject = None

    if subject_id:
        try:
            subject_id_int = int(subject_id)
        except ValueError:
            subject_id_int = None

        if subject_id_int:
            selected_subject = Subject.objects.filter(id=subject_id_int).first()

        if selected_subject:
            qs = (
                Participation.objects
                .filter(
                    role="TRAINEE",
                    training__subject=selected_subject,
                    person__is_active=True,
                )
                .values(
                    "person_id",
                    "person__sysper_id",
                    "person__first_name",
                    "person__last_name",
                    "person__email",
                    "person__contingent",
                    "person__category",
                )
                .annotate(last_completed=Max("training__end_at"))
            )

            if contingent:
                qs = qs.filter(person__contingent=contingent)
            if category:
                qs = qs.filter(person__category=category)

            # Sorting by sysper id
            if sort == "sysper_desc":
                qs = qs.order_by("-person__sysper_id")
            else:
                qs = qs.order_by("person__sysper_id")

            # Convert to list + format dates
            results = list(qs)

    return render(request, "training/training_finder.html", {
        "subjects": subjects,
        "selected_subject": selected_subject,
        "subject_id": subject_id,
        "contingent": contingent,
        "category": category,
        "sort": sort,
        "contingent_options": contingent_options,
        "category_options": category_options,
        "results": results,
    })