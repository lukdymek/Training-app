import os
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from django.utils.text import slugify
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
from django.http import HttpResponseForbidden, HttpResponseBadRequest
from functools import wraps
from django.core.exceptions import PermissionDenied
import random
from django.contrib.auth.hashers import make_password
from django.core.mail import send_mail
from django.utils import timezone
from .forms import ParticipationEditForm
import re
from django.forms import modelformset_factory
from .models import UseOfForceStandard, UofAssessment, UofRating
from .forms import UseOfForceStandardForm
from .docx_utils import fill_bookmarks
import logging
from datetime import date
from django.contrib.staticfiles import finders




CODE_TTL_MINUTES = 15
RESEND_COOLDOWN_SECONDS = 60
logger = logging.getLogger(__name__)

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
def training_detail(request, pk):
    training = get_object_or_404(Training, pk=pk)

    
    # total training duration in days (inclusive)
    # Example: Jan 1 to Jan 4 => 4 days
    duration_days = (training.end_at.date() - training.start_at.date()).days + 1
    if duration_days < 1:
        duration_days = 1

    trainees = Participation.objects.filter(
        training=training, role="TRAINEE", removed_at__isnull=True,
    ).select_related("person")

    trainers = Participation.objects.filter(
        training=training, role="TRAINER", removed_at__isnull=True,
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

    # STEP 1: exclude anyone already ACTIVE in this training (trainee or trainer)
    existing_people = Participation.objects.filter(
        training=training,
        removed_at__isnull=True,
    ).values_list("person_id", flat=True)

    available_trainers = available_trainers.exclude(id__in=existing_people)


    # ✅ exclude trainers busy in overlapping trainings (ACTIVE only)
    conflicting_people = Participation.objects.filter(
        role="TRAINER",
        removed_at__isnull=True,
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
        "is_uof": (
        training.subject is not None
        and (training.subject.name or "").strip().lower() == "use of force"
    ),
    }

    return render(request, "training/training_detail.html", context)


@login_required
@staff_required
@require_POST
def add_trainee(request, pk):
    training = get_object_or_404(Training, pk=pk)

    sysper_id = (request.POST.get("sysper_id") or "").strip()
    if not sysper_id.isdigit():
        messages.error(request, f"Invalid {SYSPER_LABEL}.")
        return redirect("training_detail", pk=pk)

    try:
        person = Person.objects.get(sysper_id=int(sysper_id))
    except Person.DoesNotExist:
        messages.error(request, "Person not found.")
        return redirect("training_detail", pk=pk)
    
    # STEP 1: If person is already ACTIVE in this training in ANY role, do not add/reactivate
    if Participation.objects.filter(
        training=training,
        person=person,
        removed_at__isnull=True,
    ).exists():
        messages.error(request, "This person is already participating in this training (as trainee or trainer).")
        return redirect("training_detail", pk=pk)


    # ✅ If previously removed as TRAINEE for this training, reactivate
    old = Participation.objects.filter(
        training=training,
        person=person,
        role="TRAINEE",
        removed_at__isnull=False,
    ).order_by("-removed_at").first()

    if old:
        old.removed_at = None
        old.removed_by = None
        old.removed_reason = ""

        try:
            old.save(update_fields=["removed_at", "removed_by", "removed_reason"])
            messages.success(request, f"{person} re-added as trainee.")
        except ValidationError:
            messages.error(
                request,
                "This person is already participating in this training in another role."
            )

        return redirect("training_detail", pk=pk)


    try:
        Participation.objects.create(training=training, person=person, role="TRAINEE")
        messages.success(request, f"{person} added as trainee.")
    except ValidationError as e:
        messages.error(request, e.messages[0] if e.messages else "Validation error.")
    except IntegrityError:
        messages.error(request, "Could not add trainee (already exists).")

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

        # ✅ If there is an old removed participation, reactivate it (so person becomes available again)
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
            old.save(update_fields=["removed_at", "removed_by", "removed_reason"])
            messages.success(request, f"{person} re-added as trainer.")
            return redirect("training_detail", pk=pk)

        # ✅ Duplicate check must only consider ACTIVE trainers
        if Participation.objects.filter(
            training=training,
            person=person,
            role="TRAINER",
            removed_at__isnull=True,
        ).exists():
            messages.error(request, "This person is already a trainer for this training.")
            return redirect("training_detail", pk=pk)

        try:
            Participation.objects.create(training=training, person=person, role="TRAINER")
            messages.success(request, f"{person} added as trainer.")
        except ValidationError as e:
            messages.error(request, e.messages[0] if e.messages else "Validation error.")
        except IntegrityError:
            # if you have a unique constraint, this catches edge cases
            messages.error(request, "Could not add trainer (already exists).")

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
    Excludes trainers that were removed from trainings.
    """
    year = request.GET.get("year", "").strip()
    month = request.GET.get("month", "").strip()

    now = datetime.now()
    y = int(year) if year.isdigit() else now.year
    m = int(month) if month.isdigit() else now.month

    start = datetime(y, m, 1)
    end = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)

    qs = (
        Participation.objects
        .filter(
            role="TRAINER",
            removed_at__isnull=True,  # ✅ IMPORTANT
            training__start_at__gte=start,
            training__start_at__lt=end,
        )
        .select_related("person")
        .values("person__id", "person__sysper_id", "person__last_name", "person__first_name")
        .annotate(total_days=Sum("days"))
        .order_by("person__last_name", "person__first_name")
    )

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

    qs = (
        Participation.objects
        .filter(
            role="TRAINER",
            removed_at__isnull=True,  # ✅ IMPORTANT
            training__start_at__gte=start,
            training__start_at__lt=end,
        )
        .select_related("person")
        .values("person__sysper_id", "person__last_name", "person__first_name")
        .annotate(total_days=Sum("days"))
        .order_by("person__last_name", "person__first_name")
    )

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

    # ✅ Make sure we only edit ACTIVE trainer participations for THIS training
    p = get_object_or_404(
        Participation,
        pk=participation_id,
        training=training,
        role="TRAINER",
        removed_at__isnull=True,  # ✅ IMPORTANT
    )

    raw = (request.POST.get("days") or "").strip().replace(",", ".")
    if raw == "":
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
        training=training,
        role=role,
        removed_at__isnull=True,   # ✅ only active blocks search results
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
def _soft_remove_participations(qs, user, reason: str):
    now = timezone.now()
    qs.update(
        removed_at=now,
        removed_by=user,
        removed_reason=(reason or "").strip(),
    )

@login_required
@staff_required
@require_POST
def remove_trainee(request, pk, person_id):
    training = get_object_or_404(Training, pk=pk)
    reason = (request.POST.get("reason") or "").strip()

    p = get_object_or_404(
        Participation,
        training=training,
        person_id=person_id,
        role="TRAINEE",
        removed_at__isnull=True,
    )

    p.removed_at = timezone.now()
    p.removed_by = request.user
    p.removed_reason = reason
    p.save(update_fields=["removed_at", "removed_by", "removed_reason"])

    # Also remove from future instances in the same recurring series (only if they exist)
    series = getattr(training, "recurring_training", None)
    if series is not None:
        Participation.objects.filter(
            person_id=person_id,
            role="TRAINEE",
            training__recurring_training=series,   # adjust if your field name differs
            training__start_at__gt=training.start_at,
            removed_at__isnull=True,
        ).update(
            removed_at=timezone.now(),
            removed_by=request.user,
            removed_reason=reason,
        )

    messages.success(request, "Trainee removed from training.")
    return redirect("training_detail", pk=pk)


@login_required
@staff_required
@require_POST
def remove_trainer(request, pk, person_id):
    training = get_object_or_404(Training, pk=pk)
    reason = (request.POST.get("reason") or "").strip()

    qs = Participation.objects.filter(
        training=training,
        person_id=person_id,
        role="TRAINER",
        removed_at__isnull=True,
    )

    updated = qs.update(
        removed_at=timezone.now(),
        removed_by=request.user,
        removed_reason=reason,
    )

    if updated == 0:
        messages.warning(request, "No active trainer participation found to remove.")
    else:
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

    start = training.start_at
    end = training.end_at

    available_qs = Person.objects.all()

    if training.subject_id:
        available_qs = available_qs.filter(trainerskill__subject=training.subject).distinct()
    else:
        available_qs = available_qs.none()

    # STEP 2: exclude anyone already ACTIVE in this training (trainee or trainer)
    existing_people = Participation.objects.filter(
        training=training,
        removed_at__isnull=True,
    ).values_list("person_id", flat=True)
    available_qs = available_qs.exclude(id__in=existing_people)


    # ✅ exclude people who are ACTIVE in overlapping trainings as TRAINER
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
        # ✅ If there is a previously removed participation for this training+person+role, reactivate it
        p = Participation.objects.filter(
            training=training,
            person=person,
            role="TRAINER",
            removed_at__isnull=False,
        ).order_by("-removed_at").first()

        if p:
            p.removed_at = None
            p.removed_by = None
            p.removed_reason = ""
            p.save(update_fields=["removed_at", "removed_by", "removed_reason"])
            reactivated += 1
            continue

        # ✅ If already participating in THIS training in any role (trainee/trainer), skip cleanly.
        # (This avoids overlap validation noise and matches what you described you want.)
        if Participation.objects.filter(
            training=training,
            person=person,
            removed_at__isnull=True,
        ).exists():
            skipped += 1
            overlap_messages.append(f"{person}: already participating in this training.")
            continue

        try:
            Participation.objects.create(training=training, person=person, role="TRAINER")
            added += 1

        except ValidationError as e:
            skipped += 1

            # Pull a useful message (your overlap logic often stores it in __all__)
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

    # Optional: show a few specific reasons (helps admins understand what happened)
    for line in overlap_messages[:3]:
        messages.warning(request, line)
    if len(overlap_messages) > 3:
        messages.warning(request, f"...and {len(overlap_messages) - 3} more.")

    return redirect("training_detail", pk=pk)


@login_required
def person_history(request, person_id):
    person = get_object_or_404(Person, pk=person_id)
    t = now()

    base_qs = (
        Participation.objects
        .filter(person=person, removed_at__isnull=True)   # ✅ only active participations
        .select_related("training", "training__subject")
    )

    completed = base_qs.filter(training__end_at__lt=t).order_by("-training__end_at", "-training__start_at")
    upcoming = base_qs.filter(training__start_at__gte=t).order_by("training__start_at")
    ongoing = base_qs.filter(training__start_at__lt=t, training__end_at__gte=t).order_by("training__end_at")

    removed = (
        Participation.objects
        .filter(person=person, removed_at__isnull=False)  # ✅ removed participations
        .select_related("training", "training__subject", "removed_by")
        .order_by("-removed_at")
    )

    return render(
        request,
        "training/person_history.html",
        {
            "person": person,
            "completed": completed,
            "upcoming": upcoming,
            "ongoing": ongoing,
            "removed": removed,   # ✅ new
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
def recurring_training(request):
    """
    Page with dropdowns (subject + filter) and a dynamic list loaded via API.
    """
    subjects = Subject.objects.filter(is_recurring=True).order_by("name")
    return render(request, "training/recurring_training.html", {"subjects": subjects})


@login_required
@staff_required
@require_GET
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

    # ✅ One row per person: latest completion for that subject
    # ✅ IMPORTANT: ignore soft-removed participations
    rows = (
        Participation.objects
        .filter(
            role="TRAINEE",
            training__subject_id=subject_id,
            person__is_active=True,
            removed_at__isnull=True,   # <-- THIS FIXES YOUR ISSUE
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
        logger.warning("EMAIL_BACKEND=%s DEFAULT_FROM_EMAIL=%s", settings.EMAIL_BACKEND, settings.DEFAULT_FROM_EMAIL)

        send_mail(
            subject="Your verification code",
            message=f"Your verification code is: {code}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,   # IMPORTANT
        )
        return code

    except Exception:
        logger.exception("Verification email send failed")  # IMPORTANT
        raise  # TEMPORARY: let it show in Railway logs while debugging
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

@require_POST
@login_required
def participation_set_status(request, participation_id):
    if not request.user.is_staff:
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    p = get_object_or_404(Participation, id=participation_id)

    new_status = (request.POST.get("status") or "").strip().upper()
    allowed = {"AUTHORISED", "PENDING", "REJECTED", "WITHDRAWN"}
    if new_status not in allowed:
        return JsonResponse({"ok": False, "error": "invalid_status"}, status=400)

    # Only update audit if status actually changed
    if p.status != new_status:
        p.status = new_status
        p.status_changed_at = timezone.now()
        p.status_changed_by = request.user
        p.save(update_fields=["status", "status_changed_at", "status_changed_by"])

    # Return info to show under the dropdown
    who = request.user.get_full_name().strip() or request.user.get_username()
    ts = timezone.localtime(p.status_changed_at).strftime("%d/%m/%Y %H:%M") if p.status_changed_at else ""

    return JsonResponse({"ok": True, "status": p.status, "changed_at": ts, "changed_by": who})

@login_required
def participation_edit(request, participation_id):
    if not request.user.is_staff:
        return render(request, "training/forbidden_nice.html", status=403)

    p = get_object_or_404(Participation, id=participation_id)

    if request.method == "POST":
        form = ParticipationEditForm(request.POST, instance=p)
        if form.is_valid():
            obj = form.save(commit=False)

            # If they typed a comment, update audit fields too (optional but helpful)
            if obj.status_comment.strip():
                obj.status_changed_at = obj.status_changed_at or timezone.now()
                obj.status_changed_by = obj.status_changed_by or request.user

            obj.save()
            messages.success(request, "Participation updated.")
            return redirect("training_detail", pk=p.training_id)
    else:
        form = ParticipationEditForm(instance=p)

    return render(request, "training/participation_edit.html", {"p": p, "form": form})

def _ensure_uof_defaults_exist():
    for ex, _ in UseOfForceStandard.EXERCISE_CHOICES:
        for ag, _ in UseOfForceStandard.AGE_GROUP_CHOICES:
            UseOfForceStandard.objects.get_or_create(
                exercise=ex,
                age_group=ag,
                defaults={
                    "age_sort": UseOfForceStandard.AGE_SORT.get(ag, 999),
                    "minimum": 0,
                    "good": 0,
                    "very_good": 0,
                },
            )




def seconds_to_mmss(seconds: int) -> str:
    s = int(seconds or 0)
    m = s // 60
    sec = s % 60
    return f"{m:02d}:{sec:02d}"

def mmss_to_seconds(value: str) -> int:
    """
    Accepts:
      - "4:20" or "04:20"
      - "4.20" (PDF format)
      - "" -> 0
    Returns integer seconds.
    """
    v = (value or "").strip()
    if not v:
        return 0

    # PDF-like "4.40"
    if "." in v and v.replace(".", "").isdigit():
        parts = v.split(".")
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = int(parts[1])
            if seconds >= 60:
                raise ValueError("Seconds must be < 60.")
            return minutes * 60 + seconds

    m = re.fullmatch(r"(\d{1,2}):(\d{2})", v)
    if not m:
        raise ValueError("Time must be MM:SS (e.g. 04:20).")

    minutes = int(m.group(1))
    seconds = int(m.group(2))
    if seconds >= 60:
        raise ValueError("Seconds must be < 60.")
    return minutes * 60 + seconds


@login_required
def uof_standards(request):
    gender = (request.GET.get("gender") or UseOfForceStandard.GENDER_MALE).upper()
    if gender not in (UseOfForceStandard.GENDER_MALE, UseOfForceStandard.GENDER_FEMALE):
        gender = UseOfForceStandard.GENDER_MALE

    grouped = []
    for ex_key, ex_label in UseOfForceStandard.EXERCISE_CHOICES:
        rows = list(
            UseOfForceStandard.objects
            .filter(gender=gender, exercise=ex_key)
            .order_by("age_sort")
        )

        # ✅ Attach display strings for RUN directly onto each row
        if ex_key == UseOfForceStandard.EXERCISE_RUN:
            for r in rows:
                r.minimum_mmss = seconds_to_mmss(r.minimum)
                r.good_mmss = seconds_to_mmss(r.good)
                r.very_good_mmss = seconds_to_mmss(r.very_good)

        grouped.append({
            "exercise_key": ex_key,
            "exercise_label": ex_label,
            "rows": rows,
        })

    if request.method == "POST":
        updated = 0

        for block in grouped:
            is_run = (block["exercise_key"] == UseOfForceStandard.EXERCISE_RUN)

            for row in block["rows"]:
                min_val = (request.POST.get(f"min_{row.id}") or "").strip()
                good_val = (request.POST.get(f"good_{row.id}") or "").strip()
                vg_val = (request.POST.get(f"vg_{row.id}") or "").strip()

                try:
                    if is_run:
                        row.minimum = mmss_to_seconds(min_val)
                        row.good = mmss_to_seconds(good_val)
                        row.very_good = mmss_to_seconds(vg_val)
                    else:
                        row.minimum = int(min_val or 0)
                        row.good = int(good_val or 0)
                        row.very_good = int(vg_val or 0)

                    row.save(update_fields=["minimum", "good", "very_good"])
                    updated += 1

                except ValueError as e:
                    messages.error(request, f"{block['exercise_label']} / {row.get_age_group_display()}: {e}")
                    return redirect(f"{request.path}?gender={gender}")

        messages.success(request, f"Saved {updated} rows.")
        return redirect(f"{request.path}?gender={gender}")

    return render(request, "training/uof_standards.html", {
        "grouped": grouped,
        "gender": gender,
    })


def parse_mmss(value: str) -> int:
    v = (value or "").strip()
    if not v:
        return 0
    # allow "4.40" or "4:40"
    if "." in v and v.replace(".", "").isdigit():
        m, s = v.split(".", 1)
        return int(m) * 60 + int(s)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", v)
    if not m:
        raise ValueError("Run time must be MM:SS (e.g. 04:20).")
    mm = int(m.group(1))
    ss = int(m.group(2))
    if ss >= 60:
        raise ValueError("Seconds must be < 60.")
    return mm * 60 + ss

def seconds_to_mmss(seconds: int) -> str:
    s = int(seconds or 0)
    return f"{s//60:02d}:{s%60:02d}"

def age_on_date(dob: datetime.date, on_date: datetime.date) -> int:
    if not dob:
        return 0
    years = on_date.year - dob.year
    if (on_date.month, on_date.day) < (dob.month, dob.day):
        years -= 1
    return years

def age_group_key(age_years: int) -> str:
    # match your UseOfForceStandard AGE_GROUP_CHOICES values
    if age_years <= 29: return "U29"
    if 30 <= age_years <= 34: return "30_34"
    if 35 <= age_years <= 39: return "35_39"
    if 40 <= age_years <= 44: return "40_44"
    if 45 <= age_years <= 49: return "45_49"
    if 50 <= age_years <= 54: return "50_54"
    if 55 <= age_years <= 59: return "55_59"
    return "60_PLUS"

def rating_for_reps(reps: int, std: UseOfForceStandard) -> str:
    # pushups/situps: higher is better
    if reps is None:
        return ""
    if reps >= std.very_good:
        return UofRating.VERY_GOOD
    if reps >= std.good:
        return UofRating.GOOD
    if reps >= std.minimum:
        return UofRating.MINIMUM
    return UofRating.FAIL

def rating_for_run(seconds: int, std: UseOfForceStandard) -> str:
    # run: lower is better (standards stored in seconds)
    if seconds is None:
        return ""
    if seconds <= std.very_good:
        return UofRating.VERY_GOOD
    if seconds <= std.good:
        return UofRating.GOOD
    if seconds <= std.minimum:
        return UofRating.MINIMUM
    return UofRating.FAIL

def compute_uof(assessment: UofAssessment) -> None:
    person = assessment.participation.person
    training = assessment.participation.training

    test_date = assessment.tested_at or (training.end_at.date() if training.end_at else timezone.now().date())
    age = age_on_date(person.dob, test_date)
    ag = age_group_key(age)

    # gender key based on your Person.gender storage
    # Adjust if your Person.gender stores something else.
    gender = "M" if (person.gender or "").upper().startswith("M") else "F"

    def get_std(ex_key: str) -> UseOfForceStandard:
        return UseOfForceStandard.objects.get(gender=gender, exercise=ex_key, age_group=ag)

    std_push = get_std("PUSHUPS")
    std_sit  = get_std("SITUPS")
    std_run  = get_std("RUN")

    assessment.pushups_rating = rating_for_reps(assessment.pushups, std_push)
    assessment.situps_rating  = rating_for_reps(assessment.situps, std_sit)
    assessment.run_rating     = rating_for_run(assessment.run_seconds, std_run)

    # PASS RULE (you can change easily later):
    # pass only if all three are at least MINIMUM
    assessment.passed = (
        assessment.pushups_rating in [UofRating.MINIMUM, UofRating.GOOD, UofRating.VERY_GOOD] and
        assessment.situps_rating  in [UofRating.MINIMUM, UofRating.GOOD, UofRating.VERY_GOOD] and
        assessment.run_rating     in [UofRating.MINIMUM, UofRating.GOOD, UofRating.VERY_GOOD]
    )

def _parse_mmss_to_seconds(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip()
    # Accept "M:SS" or "MM:SS"
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        m = int(parts[0])
        s = int(parts[1])
    except ValueError:
        return None
    if m < 0 or s < 0 or s > 59:
        return None
    return m * 60 + s


@login_required
@require_POST
def uof_save_scores(request, training_id, participation_id):
    if not request.user.is_staff:
        return HttpResponseForbidden("You do not have permission.")

    p = get_object_or_404(Participation, id=participation_id, training_id=training_id)

    # Create assessment if missing
    a, _ = UofAssessment.objects.get_or_create(
        participation=p,
        defaults={"tested_at": (p.training.end_at.date() if p.training.end_at else timezone.now().date())}
    )

    push = (request.POST.get("pushups") or "").strip()
    sit  = (request.POST.get("situps") or "").strip()

    # Accept either run_seconds OR run_time (MM:SS)
    run_seconds_raw = request.POST.get("run_seconds")
    run_time_raw = (request.POST.get("run_time") or "").strip()

    notes = (request.POST.get("notes") or "").strip()

    run_seconds = None
    if run_seconds_raw not in (None, "", "None"):
        try:
            run_seconds = int(run_seconds_raw)
        except ValueError:
            run_seconds = None
    elif run_time_raw:
        # Use your existing helper; you said parse_mmss exists already
        run_seconds = parse_mmss(run_time_raw)

    try:
        a.pushups = int(push) if push != "" else None
        a.situps  = int(sit) if sit != "" else None

        # ✅ this is the fix: use the computed run_seconds
        a.run_seconds = run_seconds

        a.notes = notes

        # compute ratings + pass/fail (your existing function)
        compute_uof(a)
        a.save()

        return JsonResponse({
            "ok": True,
            "pushups_rating": a.pushups_rating,
            "situps_rating": a.situps_rating,
            "run_rating": a.run_rating,
            "passed": a.passed,
            "run_mmss": seconds_to_mmss(a.run_seconds) if a.run_seconds is not None else "",
        })
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)

    
@login_required
@staff_required
@require_POST
def uof_update_meta(request, pk):
    training = get_object_or_404(Training, pk=pk)

    training.uof_instructor_1 = (request.POST.get("uof_instructor_1") or "").strip()
    training.uof_instructor_2 = (request.POST.get("uof_instructor_2") or "").strip()
    training.uof_chairman = (request.POST.get("uof_chairman") or "").strip()
    training.save(update_fields=["uof_instructor_1", "uof_instructor_2", "uof_chairman"])

    messages.success(request, "Use of Force instructors updated.")
    return redirect("training_detail", pk=pk)

@login_required
@staff_required
def uof_results(request, pk):
    training = get_object_or_404(Training, pk=pk)

    is_uof = (
        training.subject is not None
        and (training.subject.name or "").strip().lower() == "use of force"
    )
    missing = []
    if not (training.uof_instructor_1 or "").strip():
        missing.append("Instructor 1")
    if not (training.uof_instructor_2 or "").strip():
        missing.append("Instructor 2")
    if not (training.uof_chairman or "").strip():
        missing.append("Chairperson")

    if missing:
        messages.error(request, "Please fill in: " + ", ".join(missing) + " before opening Results.")
        return redirect("training_detail", pk=training.pk)

    if not is_uof:
        messages.error(request, "Results page is only available for Use of Force trainings.")
        return redirect("training_detail", pk=pk)

    trainees = Participation.objects.select_related("person").filter(
        training=training,
        role="TRAINEE",
    ).order_by("person__last_name", "person__first_name")

    # Attach assessment to each participation
    for p in trainees:
        assessment, _ = UofAssessment.objects.get_or_create(participation=p)
        p.assessment = assessment

    return render(
        request,
        "training/uof_results.html",
        {
            "training": training,
            "trainees": trainees,
        },
    )


def _age_on(dob: date, on_date: date) -> int:
    return on_date.year - dob.year - ((on_date.month, dob.day) < (dob.month, dob.day))


def _pretty_rating(s: str) -> str:
    # Convert values like VERY_GOOD -> Very good
    if not s:
        return ""
    s = str(s).strip()
    s = s.replace("_", " ").lower()
    return s[:1].upper() + s[1:]


@login_required
@staff_required
def uof_export_docx_one(request, training_id: int, participation_id: int):
    training = get_object_or_404(Training, pk=training_id)
    p = get_object_or_404(Participation.objects.select_related("person", "training"), pk=participation_id, training=training)
    a, _ = UofAssessment.objects.get_or_create(participation=p)

    # Ensure ratings/pass are up to date (your uof_save_scores does this already,
    # but for export we ensure the computed fields exist)
    # If you already have a compute function like compute_uof(a), call it here.
    # Example:
    # compute_uof(a)

    template_path = finders.find("Examination_card_template.docx")
    if not template_path:
        raise FileNotFoundError("Examination_card_template.docx not found in static files.")
    # If you prefer, store template under training/static/ or a dedicated folder.
    with open(template_path, "rb") as f:
        docx_bytes = f.read()

    person = p.person
    end_date = training.end_at.date()
    dob = person.dob
    age = _age_on(dob, end_date) if dob else ""

    def fmt_date(d: date | None) -> str:
        if not d:
            return ""
        return d.strftime("%m/%d/%Y")

    # Marks: default YES (customize if you have a real field)
    yes_mark, no_mark = "X", ""

    # Fill bookmarks
    fields = {
        "Iteration": "",
        "FirstName": (person.first_name or "") + "\u00A0",
        "LastName": person.last_name or "",
        "DOB": fmt_date(dob),
        "Age": str(age),
        "TestDate": fmt_date(end_date),
        "TestPlace": training.location or "",
        "Gender": (person.gender or ""),
        "OfficerFitnessYesMark": yes_mark,
        "OfficerFitnessNoMark": no_mark,

        "PushScore": "" if a.pushups is None else str(a.pushups),
        "SitScore": "" if a.situps is None else str(a.situps),
        "RunScore": "" if a.run_seconds is None else seconds_to_mmss(a.run_seconds),  # use your existing helper if you have it

        "PushResult": _pretty_rating(getattr(a, "pushups_rating", "")),
        "SitResult": _pretty_rating(getattr(a, "situps_rating", "")),
        "RunResult": _pretty_rating(getattr(a, "run_rating", "")),

        "FinalAssessment": "Passed" if getattr(a, "passed", False) else "Failed",

        "Instructor1Name": training.uof_instructor_1 or "",
        "Instructor2Name": training.uof_instructor_2 or "",
        "ChairpersonName": training.uof_chairman or "",
    }

    docx_bytes = fill_bookmarks(docx_bytes, fields)

    filename = f"uof_{training.id}_{slugify(person.last_name)}_{slugify(person.first_name)}.docx"
    resp = HttpResponse(docx_bytes, content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@login_required
@staff_required
def uof_export_docx_all_zip(request, training_id: int):
    training = get_object_or_404(Training, pk=training_id)

    trainees = Participation.objects.select_related("person").filter(training=training, role="TRAINEE").order_by(
        "person__last_name", "person__first_name"
    )

    out = BytesIO()
    with ZipFile(out, "w", compression=ZIP_DEFLATED) as z:
        for p in trainees:
            # reuse the single export by building bytes in-memory
            # simplest: call the same fill logic inline (duplicate minimal)
            a, _ = UofAssessment.objects.get_or_create(participation=p)

            template_path = finders.find("Examination_card_template.docx")
            if not template_path:
                raise FileNotFoundError("Examination_card_template.docx not found in static files.")
            with open(template_path, "rb") as f:
                docx_bytes = f.read()

            person = p.person
            end_date = training.end_at.date()
            dob = person.dob
            age = _age_on(dob, end_date) if dob else ""

            def fmt_date(d: date | None) -> str:
                if not d:
                    return ""
                return d.strftime("%m/%d/%Y")

            fields = {
                "Iteration": "",
                "FirstName": (person.first_name or "") + "\u00A0",
                "LastName": person.last_name or "",
                "DOB": fmt_date(dob),
                "Age": str(age),
                "TestDate": fmt_date(end_date),
                "TestPlace": training.location or "",
                "Gender": (person.gender or ""),
                "OfficerFitnessYesMark": "X",
                "OfficerFitnessNoMark": "",

                "PushScore": "" if a.pushups is None else str(a.pushups),
                "SitScore": "" if a.situps is None else str(a.situps),
                "RunScore": "" if a.run_seconds is None else seconds_to_mmss(a.run_seconds),

                "PushResult": _pretty_rating(getattr(a, "pushups_rating", "")),
                "SitResult": _pretty_rating(getattr(a, "situps_rating", "")),
                "RunResult": _pretty_rating(getattr(a, "run_rating", "")),

                "FinalAssessment": "Passed" if getattr(a, "passed", False) else "Failed",

                "Instructor1Name": training.uof_instructor_1 or "",
                "Instructor2Name": training.uof_instructor_2 or "",
                "ChairpersonName": training.uof_chairman or "",
            }

            docx_bytes = fill_bookmarks(docx_bytes, fields)

            fname = f"{slugify(person.last_name)}_{slugify(person.first_name)}_{person.sysper_id}.docx"
            z.writestr(fname, docx_bytes)

    resp = HttpResponse(out.getvalue(), content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="uof_training_{training.id}_cards.zip"'
    return resp

@login_required
@staff_required
def training_list(request):
    today = timezone.localdate()
    print(">>> TRAINING_LIST VIEW CALLED <<<")
    qs = (
        Training.objects.all()
        .annotate(
            trainee_count=Count(
                "participation",
                filter=Q(
                    participation__role="TRAINEE",
                    participation__removed_at__isnull=True,   # ✅ add this
                ),
                distinct=True,
            ),
            trainer_count=Count(
                "participation",
                filter=Q(
                    participation__role="TRAINER",
                    participation__removed_at__isnull=True,   # ✅ add this
                ),
                distinct=True,
            ),
        )
        .order_by("-start_at")
    )

    current_trainings = qs.filter(
        start_at__date__lte=today,
        end_at__date__gte=today,
    ).order_by("start_at")

    future_trainings = qs.filter(
        start_at__date__gt=today,
    ).order_by("start_at")

    completed_qs = qs.filter(
        end_at__date__lt=today,
    ).order_by("-start_at")

    paginator = Paginator(completed_qs, 20)
    page_number = request.GET.get("completed_page") or 1
    completed_page = paginator.get_page(page_number)

    return render(
        request,
        "training/training_list.html",
        {
            "current_trainings": current_trainings,
            "future_trainings": future_trainings,
            "completed_page": completed_page,
            "today": today,
        },
    )

