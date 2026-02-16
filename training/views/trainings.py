from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from training.constants import SYSPER_LABEL
from training.email_render import render_email_text
from training.forms import AddMultipleTrainersForm, ParticipationEditForm, TrainingForm
from training.models import Participation, Person, Subject, Training, TrainingEmailLog
from training.utils import log_status_change_email_if_needed

from .common import staff_required
from .trainers import training_total_days


@login_required
@staff_required
def training_detail(request, pk):
    training = get_object_or_404(Training, pk=pk)

    duration_days = (training.end_at.date() - training.start_at.date()).days + 1
    if duration_days < 1:
        duration_days = 1

    trainees = Participation.objects.filter(
        training=training,
        role="TRAINEE",
        removed_at__isnull=True,
    ).select_related("person")

    trainers = Participation.objects.filter(
        training=training,
        role="TRAINER",
        removed_at__isnull=True,
    ).select_related("person")

    start = training.start_at
    end = training.end_at

    subj_name = ((training.subject.name if training.subject else "") or "").strip().lower()
    is_uof = subj_name in ("use of force", "uof")

    if training.subject_id:
        available_trainers = Person.objects.filter(trainerskill__subject=training.subject).distinct()
    else:
        available_trainers = Person.objects.none()

    existing_people = Participation.objects.filter(
        training=training,
        removed_at__isnull=True,
    ).values_list("person_id", flat=True)
    available_trainers = available_trainers.exclude(id__in=existing_people)

    conflicting_people = Participation.objects.filter(
        removed_at__isnull=True,
        training__start_at__lt=end,
        training__end_at__gt=start,
    ).values_list("person_id", flat=True).distinct()
    available_trainers = available_trainers.exclude(id__in=conflicting_people)

    bulk_trainers_form = AddMultipleTrainersForm(queryset=available_trainers)

    email_logs = TrainingEmailLog.objects.filter(training=training).select_related(
        "person", "sent_by"
    ).order_by("-sent_at")

    active_parts = Participation.objects.filter(training=training, removed_at__isnull=True)

    pending_count = active_parts.filter(status="PENDING").count()
    authorised_count = active_parts.filter(status="AUTHORISED").count()
    total_participants = active_parts.count()

    assigned_logged_count = TrainingEmailLog.objects.filter(
        training=training,
        template_type="ASSIGNED",
    ).count()

    latest_email_logs = TrainingEmailLog.objects.filter(training=training).select_related(
        "person", "sent_by"
    ).order_by("-sent_at")[:10]

    participants_count = active_parts.count()

    context = {
        "training": training,
        "trainees": trainees,
        "trainers": trainers,
        "available_trainers": available_trainers,
        "bulk_trainers_form": bulk_trainers_form,
        "duration_days": duration_days,
        "is_uof": is_uof,
        "email_logs": email_logs,
        "pending_count": pending_count,
        "authorised_count": authorised_count,
        "total_participants": total_participants,
        "assigned_logged_count": assigned_logged_count,
        "participants_count": participants_count,
        "latest_email_logs": latest_email_logs,
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

    if training.capacity is not None:
        if training.capacity == 0:
            messages.error(request, "This training does not allow trainees (capacity is 0).")
            return redirect("training_detail", pk=pk)

        if training.capacity > 0:
            active_trainees = Participation.objects.filter(
                training=training,
                role="TRAINEE",
                removed_at__isnull=True,
            ).count()
            if active_trainees >= training.capacity:
                messages.error(request, f"Training is full (capacity {training.capacity} reached).")
                return redirect("training_detail", pk=pk)

    if Participation.objects.filter(
        training=training,
        person=person,
        removed_at__isnull=True,
    ).exists():
        messages.error(request, "This person is already participating in this training (as trainee or trainer).")
        return redirect("training_detail", pk=pk)

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
        old.status = "PENDING"
        old.status_changed_at = None
        old.status_changed_by = None

        try:
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
            messages.success(request, f"{person} re-added as trainee (status reset to PENDING).")
        except ValidationError as e:
            msg = e.messages[0] if getattr(e, "messages", None) else "Validation error."
            messages.error(request, msg)

        return redirect("training_detail", pk=pk)

    try:
        Participation.objects.create(training=training, person=person, role="TRAINEE")
        messages.success(request, f"{person} added as trainee.")
    except ValidationError as e:
        msg = e.messages[0] if getattr(e, "messages", None) else "Validation error."
        messages.error(request, msg)
    except IntegrityError:
        messages.error(request, "Could not add trainee (already exists).")

    return redirect("training_detail", pk=pk)


@login_required
@staff_required
def training_create(request):
    initial = {}

    start = request.GET.get("start")
    if start:
        initial["start_at"] = f"{start}T08:00"
        initial["end_at"] = f"{start}T09:00"

    if request.method == "POST":
        form = TrainingForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("calendar")
    else:
        form = TrainingForm(initial=initial)

    return render(
        request,
        "training/training_form.html",
        {
            "form": form,
            "page_title": "Create training",
            "submit_label": "Create",
        },
    )


@login_required
@staff_required
def training_edit(request, pk):
    training = get_object_or_404(Training, pk=pk)

    has_active_participants = Participation.objects.filter(
        training=training,
        removed_at__isnull=True,
    ).exists()

    locked_fields = ["course_name", "subject", "start_at", "end_at", "location", "uof_iteration"]

    if request.method == "POST":
        form = TrainingForm(request.POST, instance=training)

        if has_active_participants:
            for name in locked_fields:
                if name in form.fields:
                    form.fields[name].disabled = True

        if form.is_valid():
            if has_active_participants:
                allowed = {"capacity", "remarks"}
                changed = set(form.changed_data)
                forbidden = changed - allowed

                if forbidden:
                    messages.error(
                        request,
                        "This training already has participants. You can only change Capacity and Remarks.",
                    )
                    return redirect("training_detail", pk=pk)

                form.save()
                messages.success(request, "Saved changes.")
                return redirect("training_detail", pk=pk)

            form.save()
            messages.success(request, "Saved changes.")
            return redirect("training_detail", pk=pk)
    else:
        form = TrainingForm(instance=training)
        if has_active_participants:
            for name in locked_fields:
                if name in form.fields:
                    form.fields[name].disabled = True

    return render(
        request,
        "training/training_form.html",
        {
            "form": form,
            "training": training,
            "is_edit": True,
            "page_title": "Edit training",
            "submit_label": "Save",
            "has_active_participants": has_active_participants,
        },
    )


@login_required
@staff_required
def training_delete(request, pk):
    training = get_object_or_404(Training, pk=pk)

    active_qs = Participation.objects.filter(
        training=training,
        removed_at__isnull=True,
    )
    active_count = active_qs.count()

    if request.method == "GET" and active_count > 0:
        messages.error(
            request,
            f"Cannot delete this training: there are still {active_count} active participant(s). "
            "Remove all trainees/trainers first.",
        )
        return redirect("training_detail", pk=pk)

    if request.method == "POST":
        if active_count > 0:
            messages.error(
                request,
                f"Cannot delete this training: there are still {active_count} active participant(s). "
                "Remove all trainees/trainers first.",
            )
            return redirect("training_detail", pk=pk)

        with transaction.atomic():
            training.delete()

        messages.success(request, "Training deleted.")
        return redirect("calendar")

    return render(request, "training/training_confirm_delete.html", {"training": training})


@login_required
@staff_required
@require_GET
def people_search(request, pk):
    training = get_object_or_404(Training, pk=pk)

    role = request.GET.get("role", "TRAINEE").upper().strip()
    q = request.GET.get("q", "").strip()

    if role not in {"TRAINEE", "TRAINER"}:
        return JsonResponse({"results": []})

    if len(q) < 2:
        return JsonResponse({"results": []})

    people = Person.objects.all()

    if q.isdigit():
        people = people.filter(
            Q(sysper_id__startswith=int(q)) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
        )
    else:
        people = people.filter(Q(first_name__icontains=q) | Q(last_name__icontains=q))

    existing_person_ids = Participation.objects.filter(
        training=training,
        role=role,
        removed_at__isnull=True,
    ).values_list("person_id", flat=True)

    people = people.exclude(id__in=existing_person_ids)

    if role == "TRAINER" and training.subject_id:
        people = people.filter(trainerskill__subject=training.subject).distinct()

    people = people.order_by("last_name", "first_name")[:20]

    return JsonResponse(
        {
            "results": [
                {
                    "sysper_id": p.sysper_id,
                    "first_name": p.first_name,
                    "last_name": p.last_name,
                    "label": f"{p.last_name} {p.first_name} ({p.sysper_id})",
                }
                for p in people
            ]
        }
    )



@login_required
@staff_required
@require_POST
def remove_trainee(request, pk, person_id):
    training = get_object_or_404(Training, pk=pk)
    reason = (request.POST.get("reason") or "").strip()
    send_email = (request.POST.get("send_email") or "0") == "1"

    participation = get_object_or_404(
        Participation.objects.select_related("person"),
        training=training,
        person_id=person_id,
        role="TRAINEE",
        removed_at__isnull=True,
    )

    old_status = participation.status
    person = participation.person

    participation.removed_at = timezone.now()
    participation.removed_by = request.user
    participation.removed_reason = reason
    participation.save(update_fields=["removed_at", "removed_by", "removed_reason"])

    series = getattr(training, "recurring_training", None)
    if series is not None:
        Participation.objects.filter(
            person_id=person_id,
            role="TRAINEE",
            training__recurring_training=series,
            training__start_at__gt=training.start_at,
            removed_at__isnull=True,
        ).update(
            removed_at=timezone.now(),
            removed_by=request.user,
            removed_reason=reason,
        )

    messages.success(request, "Trainee removed from training.")

    if send_email and person.email:
        ctx = {
            "training": training,
            "person": person,
            "reason": reason,
        }

        subject = render_email_text("Removed from {{ training.course_name }}", ctx)
        body = render_email_text(
            (
                "Hello {{ person.first_name }},\n\n"
                "You have been removed from the training "
                "{{ training.course_name }}.\n\n"
                "Reason: {{ reason }}\n\n"
                "Regards,\nTraining Team"
            ),
            ctx,
        )

        TrainingEmailLog.objects.create(
            training=training,
            person=person,
            template_type="REMOVED",
            subject=subject,
            body=body,
            status_at_send=old_status,
            sent_by=request.user,
        )

    return redirect("training_detail", pk=pk)


@login_required
@staff_required
@require_POST
def remove_trainer(request, pk, person_id):
    training = get_object_or_404(Training, pk=pk)
    reason = (request.POST.get("reason") or "").strip()
    send_email = (request.POST.get("send_email") or "0") == "1"

    qs = Participation.objects.filter(
        training=training,
        person_id=person_id,
        role="TRAINER",
        removed_at__isnull=True,
    )

    participation = qs.select_related("person").first()

    updated = qs.update(
        removed_at=timezone.now(),
        removed_by=request.user,
        removed_reason=reason,
    )

    if updated == 0:
        messages.warning(request, "No active trainer participation found to remove.")
        return redirect("training_detail", pk=pk)

    messages.success(request, "Trainer removed from training.")

    if send_email and participation and participation.person.email:
        ctx = {
            "training": training,
            "person": participation.person,
            "reason": reason,
        }

        subject = render_email_text("Removed from {{ training.course_name }}", ctx)
        body = render_email_text(
            (
                "Hello {{ person.first_name }},\n\n"
                "You have been removed from the training "
                "{{ training.course_name }} ({{ training.start_at }} -> {{ training.end_at }}).\n\n"
                "Reason: {{ reason }}\n\n"
                "Regards,\nTraining Team"
            ),
            ctx,
        )

        TrainingEmailLog.objects.create(
            training=training,
            person=participation.person,
            template_type="REMOVED",
            subject=subject,
            body=body,
            status_at_send=participation.status,
            sent_by=request.user,
        )

    return redirect("training_detail", pk=pk)


@login_required
@staff_required
@require_POST
def remove_participation(request, pk, participation_id):
    training = get_object_or_404(Training, pk=pk)

    participation = get_object_or_404(
        Participation.objects.select_related("person"),
        pk=participation_id,
        training=training,
    )

    reason = (request.POST.get("reason") or "").strip()
    send_email = (request.POST.get("send_email") or "0") == "1"

    person = participation.person
    status = participation.status

    participation.delete()

    messages.success(request, "Removed from training.")

    if send_email and person.email:
        ctx = {
            "training": training,
            "person": person,
            "reason": reason,
        }

        subject = render_email_text("Removed from {{ training.course_name }}", ctx)
        body = render_email_text(
            (
                "Hello {{ person.first_name }},\n\n"
                "You have been removed from the training "
                "{{ training.course_name }}.\n\n"
                "Reason: {{ reason }}\n\n"
                "Regards,\nTraining Team"
            ),
            ctx,
        )

        TrainingEmailLog.objects.create(
            training=training,
            person=person,
            template_type="REMOVED",
            subject=subject,
            body=body,
            status_at_send=status,
            sent_by=request.user,
        )

    return redirect("training_detail", pk=pk)


@login_required
@staff_required
def recurring_training(request):
    subjects = Subject.objects.filter(is_recurring=True).order_by("name")
    return render(request, "training/recurring_training.html", {"subjects": subjects})


@login_required
@staff_required
@require_GET
def recurring_training_api(request):
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

    rows = Participation.objects.filter(
        role="TRAINEE",
        training__subject_id=subject_id,
        person__is_active=True,
        removed_at__isnull=True,
    ).values(
        "person_id",
        "person__sysper_id",
        "person__first_name",
        "person__last_name",
        "person__email",
    ).annotate(last_end_at=Max("training__end_at"))

    results = []
    for row in rows:
        last_end_at = row["last_end_at"]
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

        if window == "expired" and status != "expired":
            continue
        if window == "30" and not (0 <= days_remaining <= 30):
            continue
        if window == "90" and not (0 <= days_remaining <= 90):
            continue

        results.append(
            {
                "person_id": row["person_id"],
                "sysper_id": row["person__sysper_id"],
                "first_name": row["person__first_name"],
                "last_name": row["person__last_name"],
                "email": row["person__email"] or "",
                "last_completed": last_completed.isoformat(),
                "expires_on": expires_on.isoformat(),
                "days_remaining": days_remaining,
                "status": status,
            }
        )

    status_rank = {"expired": 0, "expiring": 1, "ok": 2}
    results.sort(key=lambda item: (status_rank.get(item["status"], 9), item["days_remaining"]))

    return JsonResponse(
        {
            "results": results,
            "meta": {
                "today": today.isoformat(),
                "validity_days": validity_days,
                "window": window,
                "subject": subject.name,
            },
        }
    )


@staff_required
@login_required
def training_finder(request):
    subjects = Subject.objects.order_by("name")

    subject_id = request.GET.get("subject_id") or ""
    contingent = request.GET.get("contingent") or ""
    category = request.GET.get("category") or ""
    sort = (request.GET.get("sort") or "sysper_asc").strip().lower()

    contingent_options = (
        Person.objects.exclude(contingent__isnull=True).exclude(contingent="").values_list(
            "contingent", flat=True
        ).distinct().order_by("contingent")
    )
    category_options = (
        Person.objects.exclude(category__isnull=True).exclude(category="").values_list(
            "category", flat=True
        ).distinct().order_by("category")
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
            qs = Participation.objects.filter(
                role="TRAINEE",
                training__subject=selected_subject,
                person__is_active=True,
            ).values(
                "person_id",
                "person__sysper_id",
                "person__first_name",
                "person__last_name",
                "person__email",
                "person__contingent",
                "person__category",
            ).annotate(last_completed=Max("training__end_at"))

            if contingent:
                qs = qs.filter(person__contingent=contingent)
            if category:
                qs = qs.filter(person__category=category)

            if sort == "sysper_desc":
                qs = qs.order_by("-person__sysper_id")
            else:
                qs = qs.order_by("person__sysper_id")

            results = list(qs)

    return render(
        request,
        "training/training_finder.html",
        {
            "subjects": subjects,
            "selected_subject": selected_subject,
            "subject_id": subject_id,
            "contingent": contingent,
            "category": category,
            "sort": sort,
            "contingent_options": contingent_options,
            "category_options": category_options,
            "results": results,
        },
    )


@require_POST
@login_required
def participation_set_status(request, participation_id):
    if not request.user.is_staff:
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    participation = get_object_or_404(Participation, id=participation_id)

    new_status = (request.POST.get("status") or "").strip().upper()
    allowed = {"AUTHORISED", "PENDING", "REJECTED", "WITHDRAWN"}
    send_email = (request.POST.get("send_email") or "0") == "1"

    if new_status not in allowed:
        return JsonResponse({"ok": False, "error": "invalid_status"}, status=400)

    if participation.status != new_status:
        participation.status = new_status
        participation.status_changed_at = timezone.now()
        participation.status_changed_by = request.user

        update_fields = ["status", "status_changed_at", "status_changed_by"]

        if participation.role == "TRAINER" and new_status == "AUTHORISED":
            total_days = training_total_days(participation.training)

            if total_days > 0:
                if participation.days is None:
                    participation.days = total_days
                    update_fields.append("days")
                else:
                    try:
                        if Decimal(participation.days) > total_days:
                            participation.days = total_days
                            update_fields.append("days")
                    except Exception:
                        participation.days = total_days
                        update_fields.append("days")

        participation.save(update_fields=update_fields)

        if new_status == "WITHDRAWN" and send_email and participation.person.email:
            ctx = {
                "training": participation.training,
                "person": participation.person,
                "reason": "",
            }

            subject = render_email_text("Withdrawn from {{ training.course_name }}", ctx)
            body = render_email_text(
                (
                    "Hello {{ person.first_name }},\n\n"
                    "You have been withdrawn from the training "
                    "{{ training.course_name }}.\n\n"
                    "If you have questions, please contact the organiser.\n\n"
                    "Regards,\nTraining Team"
                ),
                ctx,
            )

            TrainingEmailLog.objects.create(
                training=participation.training,
                person=participation.person,
                template_type="WITHDRAWN",
                subject=subject,
                body=body,
                status_at_send=participation.status,
                sent_by=request.user,
            )

        log_status_change_email_if_needed(
            training=participation.training,
            person=participation.person,
            new_status=participation.status,
            sent_by=request.user,
        )

    who = request.user.get_full_name().strip() or request.user.get_username()
    ts = (
        timezone.localtime(participation.status_changed_at).strftime("%d/%m/%Y %H:%M")
        if participation.status_changed_at
        else ""
    )

    return JsonResponse({"ok": True, "status": participation.status, "changed_at": ts, "changed_by": who})


@login_required
def participation_edit(request, participation_id):
    if not request.user.is_staff:
        return render(request, "training/forbidden_nice.html", status=403)

    participation = get_object_or_404(Participation, id=participation_id)

    if request.method == "POST":
        form = ParticipationEditForm(request.POST, instance=participation)
        if form.is_valid():
            obj = form.save(commit=False)

            if obj.status_comment.strip():
                obj.status_changed_at = obj.status_changed_at or timezone.now()
                obj.status_changed_by = obj.status_changed_by or request.user

            obj.save()
            messages.success(request, "Participation updated.")
            return redirect("training_detail", pk=participation.training_id)
    else:
        form = ParticipationEditForm(instance=participation)

    return render(request, "training/participation_edit.html", {"p": participation, "form": form})


@login_required
@staff_required
def training_list(request):
    today = timezone.localdate()
    qs = Training.objects.all().annotate(
        trainee_count=Count(
            "participation",
            filter=Q(participation__role="TRAINEE", participation__removed_at__isnull=True),
            distinct=True,
        ),
        trainer_count=Count(
            "participation",
            filter=Q(participation__role="TRAINER", participation__removed_at__isnull=True),
            distinct=True,
        ),
    ).order_by("-start_at")

    current_trainings = qs.filter(start_at__date__lte=today, end_at__date__gte=today).order_by("start_at")
    future_trainings = qs.filter(start_at__date__gt=today).order_by("start_at")
    completed_qs = qs.filter(end_at__date__lt=today).order_by("-start_at")

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


__all__ = [
    "add_trainee",
    "participation_edit",
    "participation_set_status",
    "people_search",
    "recurring_training",
    "recurring_training_api",
    "remove_participation",
    "remove_trainee",
    "remove_trainer",
    "training_create",
    "training_delete",
    "training_detail",
    "training_edit",
    "training_finder",
    "training_list",
]
