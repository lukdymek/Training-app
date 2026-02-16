import csv
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from training.email_render import render_email_text
from training.models import (
    EmailRecipient,
    EmailRecipientGroup,
    EmailTemplate,
    Participation,
    Person,
    Training,
    TrainingEmailLog,
)

from .common import staff_required


def _infer_participant_template_type(template) -> str:
    """Best-effort mapping for participant templates when no explicit type exists."""
    name = (getattr(template, "name", "") or "").upper()
    subject = (getattr(template, "subject", "") or "").upper()
    haystack = f"{name} {subject}"
    if any(token in haystack for token in ("STATUS", "REJECT", "WITHDRAW")):
        return "STATUS_CHANGE"
    return "ASSIGNED"


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
def training_email_summary(request, pk):
    training = get_object_or_404(Training, pk=pk)

    active = Participation.objects.filter(training=training, removed_at__isnull=True)

    pending = active.filter(status="PENDING").count()
    authorised = active.filter(status="AUTHORISED").count()
    total = active.count()

    return JsonResponse({
        "ok": True,
        "participants_count": total,
        "authorised_count": authorised,
        "pending_count": pending,
    })


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

    # ---- UI selections (mode, group, template) ----
    recipient_mode = (request.GET.get("mode") or request.POST.get("mode") or "admin_group").strip().lower()
    if recipient_mode not in ("participants", "trainees", "trainers", "admin_group"):
        recipient_mode = "participants"

    template_type = "ADMIN_SUMMARY" if recipient_mode == "admin_group" else "ASSIGNED"

    groups = EmailRecipientGroup.objects.filter(is_active=True).order_by("name")
    group_id = (request.GET.get("group_id") or request.POST.get("group_id") or "").strip()
    selected_group = None
    if group_id.isdigit():
        selected_group = groups.filter(id=int(group_id)).first()

    # ---- Templates list depends on recipient mode ----
    if recipient_mode == "admin_group":
        templates = EmailTemplate.objects.filter(is_active=True, kind="ADMIN").order_by("name")
    else:
        templates = EmailTemplate.objects.filter(is_active=True, kind="PARTICIPANT").order_by("name")

    template_id = request.GET.get("template_id") or request.POST.get("template_id")
    selected_template = None
    if template_id and str(template_id).isdigit():
        selected_template = templates.filter(id=int(template_id)).first()
    if recipient_mode != "admin_group" and selected_template:
        template_type = _infer_participant_template_type(selected_template)

    default_template = selected_template or (templates.first() if templates.exists() else None)
    subject_prefill = default_template.subject if default_template else ""
    body_prefill = default_template.body if default_template else ""

    subject = (request.POST.get("subject") if request.method == "POST" else None) or subject_prefill
    body = (request.POST.get("body") if request.method == "POST" else None) or body_prefill

    def compose_redirect():
        params = {
            "mode": recipient_mode,
        }
        if template_id:
            params["template_id"] = str(template_id)
        if group_id:
            params["group_id"] = str(group_id)
        url = reverse("training_email_compose", kwargs={"pk": pk})
        return redirect(f"{url}?{urlencode(params)}")

    # ---- Build recipients + status map (for participants) ----
    people_list = []
    status_map = {}
    role_map = {}

    if recipient_mode != "admin_group":
        # Roles included by mode
        if recipient_mode == "trainees":
            roles = ["TRAINEE"]
        elif recipient_mode == "trainers":
            roles = ["TRAINER"]
        else:
            roles = ["TRAINEE", "TRAINER"]

        # STATUS_CHANGE emails are for people currently in a changed terminal status.
        if template_type == "STATUS_CHANGE":
            candidates = active_parts.filter(status__in=["REJECTED", "WITHDRAWN"], role__in=roles)
        else:
            candidates = active_parts.filter(status="AUTHORISED", role__in=roles)

        # Unique people list
        people_map = {}
        for part in candidates:
            if part.person_id:
                people_map[part.person_id] = part.person
                status_map[part.person_id] = part.status
                role_map[part.person_id] = part.role  # <-- IMPORTANT
        people_list = list(people_map.values())

    # ---- Up-to-date rules ----
    def has_current_status_log(person, current_status):
        if not current_status:
            return False
        return TrainingEmailLog.objects.filter(
            training=training,
            person=person,
            template_type__in=("ASSIGNED", "STATUS_CHANGE"),
            status_at_send=current_status,
        ).exists()

    def should_log_person(person, current_status):
        # For STATUS_CHANGE, skip when the same current status was already logged.
        if template_type == "STATUS_CHANGE":
            return not has_current_status_log(person, current_status)

        # For other template types, keep duplicate-by-template behavior.
        return not TrainingEmailLog.objects.filter(
            training=training,
            person=person,
            template_type=template_type,
        ).exists()

    def has_assigned_log(person):
        return TrainingEmailLog.objects.filter(
            training=training,
            person=person,
            template_type="ASSIGNED",
        ).exists()


    def should_log_admin(rec):
        # True = should create a NEW admin log (no duplicate of same template_type + email)
        return not TrainingEmailLog.objects.filter(
            training=training,
            external_recipient_email=rec.email,
            template_type=template_type,
        ).exists()
    
    def build_admin_blocks():
        authorised = (
            active_parts
            .filter(status="AUTHORISED")
            .order_by("role", "person__last_name", "person__first_name")
        )

        trainees = authorised.filter(role="TRAINEE")
        trainers = authorised.filter(role="TRAINER")

        def fmt_people(qs):
            lines = []
            for part in qs:
                p = part.person
                if not p:
                    continue
                name = f"{p.last_name} {p.first_name}".strip()
                sysper = getattr(p, "sysper_id", "") or ""
                email = getattr(p, "email", "") or ""
                extra = []
                if sysper:
                    extra.append(f"SYSPER ID: {sysper}")
                if email:
                    extra.append(email)
                suffix = f" ({', '.join(extra)})" if extra else ""
                lines.append(f"- {name}{suffix}")
            return "\n".join(lines) if lines else "—"

        

        trainees_block = fmt_people(trainees)
        trainers_block = fmt_people(trainers)

        return  trainees_block, trainers_block

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
            up_to_date = email_ok and (not should_log_admin(rec))  # already logged

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
            current_status = status_map.get(person.id, "")
            up_to_date = email_ok and (not should_log_person(person, current_status))  # already logged

            # NEW RULE: status change only if assigned was logged before
            needs_assigned_first = (template_type == "STATUS_CHANGE")
            has_assigned = has_assigned_log(person) if needs_assigned_first else True

            if not email_ok:
                action = "skip"
                reason = "No email address"
                will_skip += 1
            elif needs_assigned_first and not has_assigned:
                action = "skip"
                reason = "No ASSIGNED email logged yet"
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
                trainees_block, trainers_block = build_admin_blocks()
                ctx = {
                    "training": training,
                    "person": None,
                    "status": "",
                    "recipient_name": rec.name,
                    "recipient_email": rec.email,
                    "trainees_block": trainees_block,
                    "trainers_block": trainers_block,
                }
                preview_subject = render_email_text(subject, ctx)
                preview_body = render_email_text(body, ctx)
        else:
            preview_person = people_list[0] if people_list else None
            if preview_person:
                _role = role_map.get(preview_person.id, "")
                ctx = {
                    "training": training,
                    "person": preview_person,
                    "status": status_map.get(preview_person.id, ""),
                    "role": _role,
                    "role_label": (_role or "").replace("_", " ").title(),  # e.g. TRAINEE -> Trainee
                    "role_map": role_map,

                }

                preview_subject = render_email_text(subject, ctx)
                preview_body = render_email_text(body, ctx)

    # ---- POST: create logs (no real sending) ----
    if request.method == "POST":

        # Admin group requires a selected group
        if recipient_mode == "admin_group" and not group_id:
            messages.error(request, "Please select an admin group before sending/logging.")
            return compose_redirect()


        # Only block on PENDING for participant emails
        if recipient_mode != "admin_group" and pending_count > 0:
            messages.error(
                request,
                f"Cannot send emails: {pending_count} participant(s) are still PENDING. Please resolve statuses first."
            )
            return compose_redirect()

        if will_send <= 0:
            messages.error(request, "Nothing to send/log (all recipients are missing email or already up to date).")
            return compose_redirect()

        created = 0
        skipped = 0
        skipped_no_email = 0
        skipped_no_assigned = 0

        if recipient_mode == "admin_group":
            for row in recipient_rows:
                rec = row["recipient"]
                if not (rec.email or "").strip():
                    skipped_no_email += 1
                    continue
                if not should_log_admin(rec):
                    skipped += 1
                    continue

                trainees_block, trainers_block = build_admin_blocks()
                ctx = {
                    "training": training,
                    "person": None,
                    "status": "",
                    "recipient_name": rec.name,
                    "recipient_email": rec.email,
                    "trainees_block": trainees_block,
                    "trainers_block": trainers_block,
                }

                rendered_subject = render_email_text(subject, ctx)
                rendered_body = render_email_text(body, ctx)

                log = TrainingEmailLog.objects.create(
                    training=training,
                    person=None,
                    external_recipient_name=rec.name,
                    external_recipient_email=rec.email,
                    template_type="ADMIN_SUMMARY",
                    subject=rendered_subject,
                    body=rendered_body,
                    status_at_send="",
                    sent_by=request.user,
                    delivery_status="LOGGED",
                    error_message="",
                    sent_to=rec.email,
                )

                created += 1

        else:
            for person in people_list:
                if not (person.email or "").strip():
                    skipped_no_email += 1
                    continue

                # STATUS_CHANGE requires an ASSIGNED baseline first
                if template_type == "STATUS_CHANGE" and not has_assigned_log(person):
                    skipped_no_assigned += 1
                    continue

                current_status = status_map.get(person.id, "")
                if not should_log_person(person, current_status):
                    skipped += 1
                    continue

                _role = role_map.get(person.id, "")
                ctx = {
                    "training": training,
                    "person": person,
                    "status": current_status,
                    "role": _role,
                    "role_label": (_role or "").replace("_", " ").title(),
                }
                rendered_subject = render_email_text(subject, ctx)
                rendered_body = render_email_text(body, ctx)

                log = TrainingEmailLog.objects.create(
                    training=training,
                    person=person,
                    external_recipient_name="",
                    external_recipient_email="",
                    template_type=template_type,
                    subject=rendered_subject,
                    body=rendered_body,
                    status_at_send=current_status,
                    sent_by=request.user,
                    delivery_status="LOGGED",
                    error_message="",
                    sent_to=person.email,
                )

                created += 1

        if created:
            messages.success(request, f"Logged emails for {created} recipient(s).")
        if skipped:
            messages.warning(request, f"Skipped {skipped} recipient(s) (already up to date).")
        if skipped_no_email:
            messages.warning(request, f"Skipped {skipped_no_email} recipient(s) (no email address).")
        if skipped_no_assigned:
            messages.warning(request, f"Skipped {skipped_no_assigned} recipient(s) (no ASSIGNED email logged yet).")

        # Redirect to logs so you can immediately see what was created
        return redirect("training_email_logs", pk=pk)

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
                str(selected_template.id)
                if selected_template
                else (str(default_template.id) if default_template else "")
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
    if type_filter in ("ASSIGNED", "STATUS_CHANGE", "ADMIN_SUMMARY"):
        qs = qs.filter(template_type=type_filter)

    # Only applies to participant logs (person-based logs)
    if sysper.isdigit():
        qs = qs.filter(person__sysper_id=int(sysper))

    if sent_by.isdigit():
        qs = qs.filter(sent_by_id=int(sent_by))

    # Search across subject/body + person + external recipients
    if q:
        qs = qs.filter(
            Q(subject__icontains=q) |
            Q(body__icontains=q) |
            Q(person__first_name__icontains=q) |
            Q(person__last_name__icontains=q) |
            Q(person__email__icontains=q) |
            Q(external_recipient_name__icontains=q) |
            Q(external_recipient_email__icontains=q)
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
            "template_id": str(default_template.id) if default_template else "",
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


__all__ = [
    "training_email_admin_compose",
    "training_email_compose",
    "training_email_log_assigned",
    "training_email_log_detail",
    "training_email_logs",
    "training_email_logs_export_csv",
    "training_email_resend_one",
    "training_email_summary",
]
