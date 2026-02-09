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
from .models import Person, TrainerSkill, Subject, Participation, Training, EmailVerification, TrainingEmailLog, EmailRecipientGroup, EmailRecipient
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
from training.utils import log_status_change_email_if_needed
from .models import EmailTemplate
from .email_render import render_email_text
from django.views.decorators.http import require_http_methods



CODE_TTL_MINUTES = 15
RESEND_COOLDOWN_SECONDS = 60
logger = logging.getLogger(__name__)


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

@login_required
@staff_required
@require_POST
def training_email_log_assigned(request, pk):
    training = get_object_or_404(Training, pk=pk)

    # Block if any PENDING participants exist (trainees or trainers)
    if Participation.objects.filter(training=training, status="PENDING", removed_at__isnull=True).exists():
        messages.error(request, "Cannot send emails: some participants are still PENDING. Please resolve them first.")
        return redirect("training_detail", pk=pk)

    # Select active AUTHORISED participants
    participants = (
        Participation.objects
        .filter(training=training, removed_at__isnull=True, status="AUTHORISED")
        .select_related("person")
    )

    created = 0
    skipped = 0

    for part in participants:
        person = part.person

        # Don’t duplicate: if already logged as ASSIGNED, skip
        if TrainingEmailLog.objects.filter(training=training, person=person, template_type="ASSIGNED").exists():
            skipped += 1
            continue

        TrainingEmailLog.objects.create(
            training=training,
            person=person,
            template_type="ASSIGNED",
            subject=f"Assigned to training: {training.course_name}",
            body=f"(stub) You have been assigned to {training.course_name} from {training.start_at} to {training.end_at}.",
            status_at_send=part.status,
            sent_by=request.user,
        )
        created += 1

    if created:
        messages.success(request, f"Logged ASSIGNED email for {created} participant(s).")
    if skipped:
        messages.warning(request, f"Skipped {skipped} participant(s) (already logged).")

    return redirect("training_detail", pk=pk)




@login_required
@staff_required
def training_email_compose(request, pk):
    training = get_object_or_404(Training, pk=pk)

    # ---- Active participations (used for participant modes) ----
    active_parts = (
        Participation.objects
        .filter(training=training, removed_at__isnull=True)
        .select_related("person")
    )
    pending_count = active_parts.filter(status="PENDING").count()

    # ---- UI selections (mode, group, template type, template) ----
    recipient_mode = (request.GET.get("mode") or request.POST.get("mode") or "participants").strip().lower()
    if recipient_mode not in ("participants", "trainees", "trainers", "admin_group"):
        recipient_mode = "participants"

    template_type = (request.GET.get("template_type") or request.POST.get("template_type") or "ASSIGNED").strip().upper()
    if template_type not in ("ASSIGNED", "STATUS_CHANGE", "ADMIN_SUMMARY"):
        template_type = "ASSIGNED"

    groups = EmailRecipientGroup.objects.filter(is_active=True).order_by("name")
    group_id = (request.GET.get("group_id") or request.POST.get("group_id") or "").strip()
    selected_group = None
    if group_id.isdigit():
        selected_group = groups.filter(id=int(group_id)).first()

    # ---- Templates list depends on mode/type ----
    # You can keep it simple: admin_group uses ADMIN templates, participants use PARTICIPANT templates
    if recipient_mode == "admin_group" or template_type == "ADMIN_SUMMARY":
        templates = EmailTemplate.objects.filter(is_active=True, kind="ADMIN").order_by("name")
    else:
        templates = EmailTemplate.objects.filter(is_active=True, kind="PARTICIPANT").order_by("name")

    template_id = request.GET.get("template_id") or request.POST.get("template_id")
    selected_template = None
    if template_id and str(template_id).isdigit():
        selected_template = templates.filter(id=int(template_id)).first()

    default_template = selected_template or (templates.first() if templates.exists() else None)
    subject_prefill = default_template.subject if default_template else ""
    body_prefill = default_template.body if default_template else ""

    subject = (request.POST.get("subject") if request.method == "POST" else None) or subject_prefill
    body = (request.POST.get("body") if request.method == "POST" else None) or body_prefill

    # ---- Build recipients + status map (for participants) ----
    people_list = []
    status_map = {}

    if recipient_mode != "admin_group":
        # Roles included by mode
        if recipient_mode == "trainees":
            roles = ["TRAINEE"]
        elif recipient_mode == "trainers":
            roles = ["TRAINER"]
        else:
            roles = ["TRAINEE", "TRAINER"]

        candidates = active_parts.filter(status="AUTHORISED", role__in=roles)

        # Unique people list
        people_map = {}
        for part in candidates:
            people_map[part.person_id] = part.person
            status_map[part.person_id] = part.status
        people_list = list(people_map.values())

    # ---- Up-to-date rules ----
    def should_log_person(person):
        # Skip duplicates of same template_type for same person+training
        return not TrainingEmailLog.objects.filter(
            training=training,
            person=person,
            template_type=template_type,
        ).exists()

    def should_log_admin(rec):
        # IMPORTANT: use external_recipient_email (NOT recipient_email)
        return not TrainingEmailLog.objects.filter(
            training=training,
            external_recipient_email=rec.email,
            template_type=template_type,
        ).exists()

    # ---- Build recipient_rows + counts ----
    recipient_rows = []
    will_send = 0
    will_skip = 0

    if recipient_mode == "admin_group":
        recipients = EmailRecipient.objects.none()
        if selected_group:
            recipients = selected_group.recipients.filter(is_active=True).order_by("name")

        for rec in recipients:
            email_ok = bool((rec.email or "").strip())
            up_to_date = email_ok and (not should_log_admin(rec))

            if not email_ok:
                action = "skip"
                reason = "No email address"
                will_skip += 1
            elif up_to_date:
                action = "skip"
                reason = "Already up to date"
                will_skip += 1
            else:
                action = "send"
                reason = "Will be logged"
                will_send += 1

            recipient_rows.append({
                "type": "admin",
                "recipient": rec,
                "action": action,
                "reason": reason,
            })

    else:
        for person in people_list:
            email_ok = bool((person.email or "").strip())
            up_to_date = email_ok and (not should_log_person(person))

            if not email_ok:
                action = "skip"
                reason = "No email address"
                will_skip += 1
            elif up_to_date:
                action = "skip"
                reason = "Already up to date"
                will_skip += 1
            else:
                action = "send"
                reason = "Will be logged"
                will_send += 1

            recipient_rows.append({
                "type": "person",
                "person": person,
                "action": action,
                "reason": reason,
            })

    # ---- Preview (GET only): render for first recipient ----
    preview_person = None
    preview_subject = ""
    preview_body = ""

    if request.method != "POST":
        if recipient_mode == "admin_group":
            if recipient_rows:
                rec = recipient_rows[0]["recipient"]
                ctx = {
                    "training": training,
                    "person": None,
                    "status": "",
                    "recipient_name": rec.name,
                    "recipient_email": rec.email,
                }
                preview_subject = render_email_text(subject, ctx)
                preview_body = render_email_text(body, ctx)
        else:
            preview_person = people_list[0] if people_list else None
            if preview_person:
                ctx = {
                    "training": training,
                    "person": preview_person,
                    "status": status_map.get(preview_person.id, ""),
                }
                preview_subject = render_email_text(subject, ctx)
                preview_body = render_email_text(body, ctx)

    # ---- POST: create logs (no real sending) ----
    if request.method == "POST":
        # Only block on PENDING for participant emails (as you requested earlier)
        if recipient_mode != "admin_group" and pending_count > 0:
            messages.error(
                request,
                f"Cannot send emails: {pending_count} participant(s) are still PENDING. Please resolve statuses first."
            )
            return redirect("training_email_compose", pk=pk)

        if will_send <= 0:
            messages.error(request, "Nothing to send/log (all recipients are missing email or already up to date).")
            return redirect("training_email_compose", pk=pk)

        created = 0
        skipped = 0
        skipped_no_email = 0

        if recipient_mode == "admin_group":
            for row in recipient_rows:
                rec = row["recipient"]
                if not (rec.email or "").strip():
                    skipped_no_email += 1
                    continue
                if not should_log_admin(rec):
                    skipped += 1
                    continue

                ctx = {
                    "training": training,
                    "person": None,
                    "status": "",
                    "recipient_name": rec.name,
                    "recipient_email": rec.email,
                }
                rendered_subject = render_email_text(subject, ctx)
                rendered_body = render_email_text(body, ctx)

                TrainingEmailLog.objects.create(
                    training=training,
                    person=None,
                    external_recipient_name=rec.name,
                    external_recipient_email=rec.email,
                    template_type=template_type,  # typically ADMIN_SUMMARY
                    subject=rendered_subject,
                    body=rendered_body,
                    status_at_send="",
                    sent_by=request.user,
                )
                created += 1

        else:
            for person in people_list:
                if not (person.email or "").strip():
                    skipped_no_email += 1
                    continue
                if not should_log_person(person):
                    skipped += 1
                    continue

                ctx = {
                    "training": training,
                    "person": person,
                    "status": status_map.get(person.id, ""),
                }
                rendered_subject = render_email_text(subject, ctx)
                rendered_body = render_email_text(body, ctx)

                TrainingEmailLog.objects.create(
                    training=training,
                    person=person,
                    external_recipient_name="",
                    external_recipient_email="",
                    template_type=template_type,
                    subject=rendered_subject,
                    body=rendered_body,
                    status_at_send=status_map.get(person.id, ""),
                    sent_by=request.user,
                )
                created += 1

        if created:
            messages.success(request, f"Logged emails for {created} recipient(s).")
        if skipped:
            messages.warning(request, f"Skipped {skipped} recipient(s) (already up to date).")
        if skipped_no_email:
            messages.warning(request, f"Skipped {skipped_no_email} recipient(s) (no email address).")

        return redirect("training_detail", pk=pk)

    return render(
        request,
        "training/training_email_compose.html",
        {
            "training": training,
            "pending_count": pending_count,
            "recipient_mode": recipient_mode,
            "template_type": template_type,

            # admin groups
            "groups": groups,
            "group_id": group_id,
            "selected_group": selected_group,

            # templates
            "templates": templates,
            "selected_template": selected_template,
            "template_id": (
                selected_template.id
                if selected_template
                else (default_template.id if default_template else "")
            ),

            # message content
            "subject": subject,
            "body": body,

            # recipients preview
            "people": people_list,
            "recipient_rows": recipient_rows,
            "will_send": will_send,
            "will_skip": will_skip,

            # preview
            "preview_person": preview_person,
            "preview_subject": preview_subject,
            "preview_body": preview_body,
        },
    )


@login_required
@staff_required
def training_email_logs(request, pk):
    training = get_object_or_404(Training, pk=pk)

    type_filter = (request.GET.get("type") or "").strip().upper()
    sysper = (request.GET.get("sysper") or "").strip()
    sent_by = (request.GET.get("sent_by") or "").strip()
    q = (request.GET.get("q") or "").strip()

    qs = (
        TrainingEmailLog.objects
        .filter(training=training)
        .select_related("person", "sent_by")
        .order_by("-sent_at")
    )

    # ---- Filters ----
    if type_filter in ("ASSIGNED", "STATUS_CHANGE"):
        qs = qs.filter(template_type=type_filter)

    if sysper.isdigit():
        qs = qs.filter(person__sysper_id=int(sysper))

    if sent_by.isdigit():
        qs = qs.filter(sent_by_id=int(sent_by))

    if q:
        qs = qs.filter(
            Q(subject__icontains=q) |
            Q(body__icontains=q) |
            Q(person__first_name__icontains=q) |
            Q(person__last_name__icontains=q)
        )
    # ---- Pagination ----
    paginator = Paginator(qs, 25)   # 25 logs per page
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # ---- Sender dropdown values ----
    senders = (
        TrainingEmailLog.objects
        .filter(training=training, sent_by__isnull=False)
        .values(
            "sent_by_id",
            "sent_by__first_name",
            "sent_by__last_name",
            "sent_by__username",
        )
        .distinct()
        .order_by(
            "sent_by__last_name",
            "sent_by__first_name",
            "sent_by__username",
        )
    )

    return render(
        request,
        "training/training_email_logs.html",
        {
            "training": training,

            # paginated rows
            "rows": page_obj,
            "page_obj": page_obj,

            # filters
            "type_filter": type_filter,
            "sysper": sysper,
            "sent_by": sent_by,

            # dropdown data
            "senders": senders,

            "q": q,

        },
    )



@login_required
@staff_required
def training_email_log_detail(request, pk, log_id):
    training = get_object_or_404(Training, pk=pk)

    log = get_object_or_404(
        TrainingEmailLog.objects.select_related("person", "sent_by"),
        pk=log_id,
        training=training,
    )

    return render(
        request,
        "training/training_email_log_detail.html",
        {
            "training": training,
            "log": log,
        },
    )

@login_required
@staff_required
def training_email_logs_export_csv(request, pk):
    training = get_object_or_404(Training, pk=pk)

    type_filter = (request.GET.get("type") or "").strip().upper()
    sysper = (request.GET.get("sysper") or "").strip()
    sent_by = (request.GET.get("sent_by") or "").strip()
    q = (request.GET.get("q") or "").strip()

    qs = (
        TrainingEmailLog.objects
        .filter(training=training)
        .select_related("person", "sent_by")
        .order_by("-sent_at")
    )

    # --- Filters ---
    if type_filter in ("ASSIGNED", "STATUS_CHANGE"):
        qs = qs.filter(template_type=type_filter)

    if sysper.isdigit():
        qs = qs.filter(person__sysper_id=int(sysper))

    if sent_by.isdigit():
        qs = qs.filter(sent_by_id=int(sent_by))

    # ✅ Search
    if q:
        qs = qs.filter(
            Q(subject__icontains=q) |
            Q(body__icontains=q) |
            Q(person__first_name__icontains=q) |
            Q(person__last_name__icontains=q)
        )

    # --- CSV ---
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = (
        f'attachment; filename="email_logs_training_{training.id}.csv"'
    )

    w = csv.writer(resp)
    w.writerow([
        "Sent at",
        "Template type",
        "Recipient SYSPER ID",
        "Recipient last name",
        "Recipient first name",
        "Recipient email",
        "Status at send",
        "Sent by",
        "Subject",
        "Body",
    ])

    for r in qs:
        sender_name = ""
        if r.sent_by:
            sender_name = (
                r.sent_by.get_full_name()
                or r.sent_by.username
                or ""
            ).strip()

        w.writerow([
            r.sent_at,
            r.template_type,
            r.person.sysper_id if r.person else "",
            r.person.last_name if r.person else "",
            r.person.first_name if r.person else "",
            r.person.email if r.person else "",
            r.status_at_send or "",
            sender_name,
            r.subject or "",
            r.body or "",
        ])

    return resp

@login_required
@staff_required
@require_POST
def training_email_resend_one(request, pk, person_id):
    training = get_object_or_404(Training, pk=pk)
    person = get_object_or_404(Person, pk=person_id)

    template_type = (request.POST.get("template_type") or "ASSIGNED").strip().upper()
    if template_type not in ("ASSIGNED", "STATUS_CHANGE"):
        template_type = "ASSIGNED"

    subject = (request.POST.get("subject") or "").strip()
    body = (request.POST.get("body") or "").strip()

    # Only allow if the person is currently active+authorised on this training (any role)
    part = Participation.objects.filter(
        training=training,
        person=person,
        removed_at__isnull=True,
        status="AUTHORISED",
    ).first()

    if not part:
        messages.error(request, "Cannot resend: person is not currently AUTHORISED on this training.")
        return redirect("training_email_logs", pk=pk)

    if not (person.email or "").strip():
        messages.error(request, "Cannot resend: no email address for this person.")
        return redirect("training_email_logs", pk=pk)

    ctx = {"training": training, "person": person, "status": part.status}
    rendered_subject = render_email_text(subject, ctx)
    rendered_body = render_email_text(body, ctx)

    TrainingEmailLog.objects.create(
        training=training,
        person=person,
        template_type=template_type,
        subject=rendered_subject,
        body=rendered_body,
        status_at_send=part.status,
        sent_by=request.user,
    )

    messages.success(request, f"Resend logged for {person}.")
    return redirect("training_email_logs", pk=pk)


@login_required
@staff_required
def training_email_admin_compose(request, pk):
    training = get_object_or_404(Training, pk=pk)

    # --- participations (ACTIVE only) ---
    active_parts = (
        Participation.objects
        .filter(training=training, removed_at__isnull=True)
        .select_related("person")
    )

    pending_count = active_parts.filter(status="PENDING").count()

    # Only AUTHORISED in the admin summary (your decision)
    authorised_parts = (
        active_parts
        .filter(status="AUTHORISED")
        .order_by("role", "person__last_name", "person__first_name")
    )

    # --- recipients: group or single ---
    groups = EmailRecipientGroup.objects.filter(is_active=True).order_by("name")
    singles = EmailRecipient.objects.filter(is_active=True).order_by("name")

    recipient_mode = (request.GET.get("rec_mode") or request.POST.get("rec_mode") or "group").strip().lower()
    if recipient_mode not in ("group", "single"):
        recipient_mode = "group"

    group_id = (request.GET.get("group_id") or request.POST.get("group_id") or "").strip()
    recipient_id = (request.GET.get("recipient_id") or request.POST.get("recipient_id") or "").strip()

    selected_group = groups.filter(id=int(group_id)).first() if group_id.isdigit() else None
    selected_recipient = singles.filter(id=int(recipient_id)).first() if recipient_id.isdigit() else None

    # Resolve final recipients (list of EmailRecipient)
    if recipient_mode == "single":
        recipient_list = [selected_recipient] if selected_recipient else []
    else:
        recipient_list = list(selected_group.recipients.filter(is_active=True)) if selected_group else []

    # --- templates (ADMIN kind) ---
    templates = EmailTemplate.objects.filter(is_active=True, kind="ADMIN").order_by("name")

    template_id = request.GET.get("template_id") or request.POST.get("template_id") or ""
    selected_template = templates.filter(id=int(template_id)).first() if str(template_id).isdigit() else None
    default_template = selected_template or (templates.first() if templates.exists() else None)

    subject_prefill = default_template.subject if default_template else ""
    body_prefill = default_template.body if default_template else ""

    subject = (request.POST.get("subject") if request.method == "POST" else None) or subject_prefill
    body = (request.POST.get("body") if request.method == "POST" else None) or body_prefill

    # --- build participant summary text (AUTHORISED only) ---
    def build_participant_lines(qs):
        trainers = qs.filter(role="TRAINER")
        trainees = qs.filter(role="TRAINEE")

        lines = []
        lines.append("TRAINERS (AUTHORISED):")
        if trainers.exists():
            for p in trainers:
                person = p.person
                email = (person.email or "").strip()
                days = f", days: {p.days}" if p.days is not None else ""
                lines.append(
                    f"- {person.last_name} {person.first_name} (SYSPER ID: {person.sysper_id}{days})"
                    + (f" <{email}>" if email else "")
                )
        else:
            lines.append("- (none)")

        lines.append("")  # blank line

        lines.append("TRAINEES (AUTHORISED):")
        if trainees.exists():
            for p in trainees:
                person = p.person
                email = (person.email or "").strip()
                lines.append(
                    f"- {person.last_name} {person.first_name} (SYSPER ID: {person.sysper_id})"
                    + (f" <{email}>" if email else "")
                )
        else:
            lines.append("- (none)")

        return "\n".join(lines)

    participants_text = build_participant_lines(authorised_parts)

    # --- preview (GET): show rendered message for first recipient ---
    preview_to = recipient_list[0] if recipient_list else None
    preview_subject = ""
    preview_body = ""
    if preview_to:
        ctx = {
            "training": training,
            "recipient_name": preview_to.name,
            "recipient_email": preview_to.email,
            "participants": participants_text,
        }
        preview_subject = render_email_text(subject, ctx)
        preview_body = render_email_text(body, ctx)

    # --- counts for UI ---
    will_send = 0
    will_skip = 0

    def should_log_admin(recipient):
        # Up-to-date rule: if we already logged ADMIN_SUMMARY to this email, skip
        return not TrainingEmailLog.objects.filter(
            training=training,
            template_type="ADMIN_SUMMARY",
            external_recipient_email=recipient.email,
        ).exists()

    recipient_rows = []
    for r in recipient_list:
        email_ok = bool((r.email or "").strip())
        up_to_date = email_ok and (not should_log_admin(r))

        if not email_ok:
            action = "skip"
            reason = "No email address"
            will_skip += 1
        elif up_to_date:
            action = "skip"
            reason = "Already up to date"
            will_skip += 1
        else:
            action = "send"
            reason = "Will be logged"
            will_send += 1

        recipient_rows.append({
            "recipient": r,
            "action": action,
            "reason": reason,
        })

    # --- POST: create logs (log-only) ---
    if request.method == "POST":
        if pending_count > 0:
            messages.error(
                request,
                f"Cannot send admin email: {pending_count} participant(s) are still PENDING. Resolve statuses first."
            )
            return redirect("training_email_admin_compose", pk=pk)

        if not recipient_list:
            messages.error(request, "Please select a recipient group or a single recipient.")
            return redirect("training_email_admin_compose", pk=pk)

        created = 0
        skipped = 0
        skipped_no_email = 0

        for r in recipient_list:
            if not (r.email or "").strip():
                skipped_no_email += 1
                continue

            if not should_log_admin(r):
                skipped += 1
                continue

            ctx = {
                "training": training,
                "recipient_name": r.name,
                "recipient_email": r.email,
                "participants": participants_text,
            }
            rendered_subject = render_email_text(subject, ctx)
            rendered_body = render_email_text(body, ctx)

            TrainingEmailLog.objects.create(
                training=training,
                template_type="ADMIN_SUMMARY",
                external_recipient_name=r.name,
                external_recipient_email=r.email,
                subject=rendered_subject,
                body=rendered_body,
                status_at_send="AUTHORISED",
                sent_by=request.user,
            )
            created += 1

        if created:
            messages.success(request, f"Logged admin email(s): {created}.")
        if skipped:
            messages.warning(request, f"Skipped {skipped} recipient(s) (already up to date).")
        if skipped_no_email:
            messages.warning(request, f"Skipped {skipped_no_email} recipient(s) (no email address).")

        return redirect("training_detail", pk=pk)

    return render(
        request,
        "training/training_email_admin_compose.html",
        {
            "training": training,
            "pending_count": pending_count,

            "templates": templates,
            "template_id": default_template.id if default_template else "",
            "selected_template": selected_template,

            "subject": subject,
            "body": body,

            "groups": groups,
            "singles": singles,
            "recipient_mode": recipient_mode,
            "group_id": selected_group.id if selected_group else "",
            "recipient_id": selected_recipient.id if selected_recipient else "",

            "recipient_list": recipient_list,
            "recipient_rows": recipient_rows,
            "participants_text": participants_text,

            "preview_to": preview_to,
            "preview_subject": preview_subject,
            "preview_body": preview_body,

            "will_send": will_send,
            "will_skip": will_skip,
        },
    )

def _fmt_dt(dt):
    """Small helper: format datetimes nicely (local time)."""
    if not dt:
        return ""
    return timezone.localtime(dt).strftime("%d/%m/%Y %H:%M")


def build_admin_summary_blocks(training):
    """
    Returns text blocks for admin summary email.
    Includes ONLY AUTHORISED participants (trainees/trainers),
    and excludes removed participations.
    """
    qs = (
        Participation.objects
        .filter(
            training=training,
            removed_at__isnull=True,
            status="AUTHORISED",
        )
        .select_related("person")
        .order_by("role", "person__last_name", "person__first_name")
    )

    trainees = qs.filter(role="TRAINEE")
    trainers = qs.filter(role="TRAINER")

    # Build trainees block
    trainees_lines = []
    for p in trainees:
        trainees_lines.append(f"- {p.person.last_name} {p.person.first_name} (SYSPER ID: {p.person.sysper_id})")

    trainees_block = "Trainees (AUTHORISED):\n"
    trainees_block += "\n".join(trainees_lines) if trainees_lines else "— none —"

    # Build trainers block (include days if present)
    trainers_lines = []
    for p in trainers:
        days_txt = f", days: {p.days}" if p.days is not None else ""
        trainers_lines.append(f"- {p.person.last_name} {p.person.first_name} (SYSPER ID: {p.person.sysper_id}{days_txt})")

    trainers_block = "Trainers (AUTHORISED):\n"
    trainers_block += "\n".join(trainers_lines) if trainers_lines else "— none —"

    # Stats block
    participants_count = qs.count()
    trainees_count = trainees.count()
    trainers_count = trainers.count()

    stats_block = (
        "Summary:\n"
        f"- Training: {training.course_name}\n"
        f"- When: {_fmt_dt(training.start_at)} → {_fmt_dt(training.end_at)}\n"
        f"- Where: {training.location or '—'}\n"
        f"- Capacity: {training.capacity} (AUTHORISED total: {participants_count})\n"
        f"- Authorised trainees: {trainees_count}\n"
        f"- Authorised trainers: {trainers_count}\n"
    )

    return {
        "trainees_block": trainees_block,
        "trainers_block": trainers_block,
        "stats_block": stats_block,
    }
