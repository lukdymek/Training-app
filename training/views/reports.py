import csv
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.utils.timezone import localtime

from training.constants import SYSPER_LABEL
from training.models import Participation, Person

from .common import staff_required


def _is_uof_training(participation):
    subject = getattr(participation.training, "subject", None)
    name = (getattr(subject, "name", "") or "").strip().lower()
    return name in ("use of force", "uof")


def _uof_completion_display(participation):
    try:
        assessment = participation.uof_assessment
    except Exception:
        return ""

    has_scores = any(
        value is not None and value != ""
        for value in (assessment.pushups, assessment.situps, assessment.run_seconds)
    )
    if not has_scores:
        return ""
    return "PASS" if assessment.passed else "FAIL"


def _with_completion_display(rows):
    for p in rows:
        if _is_uof_training(p):
            p.completion_status_display = _uof_completion_display(p)
        else:
            p.completion_status_display = p.completion_status or ""
    return rows


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
            rows = (
                Participation.objects.filter(person=person, removed_at__isnull=True)
                .select_related("training", "training__subject", "uof_assessment")
                .order_by("-training__start_at")
            )
            rows = _with_completion_display(list(rows))

    return render(
        request,
        "training/report_person.html",
        {
            "sysper": sysper,
            "person": person,
            "rows": rows,
        },
    )


@login_required
@staff_required
def report_person_export(request):
    sysper = request.GET.get("sysper_id", "").strip()
    if not sysper.isdigit():
        return HttpResponse("Invalid sysper_id", status=400)

    person = Person.objects.filter(sysper_id=int(sysper)).first()
    if not person:
        return HttpResponse("Person not found", status=404)

    rows = (
        Participation.objects.filter(person=person)
        .select_related("training", "training__subject")
        .order_by("-training__start_at")
    )

    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="person_{person.sysper_id}_history.csv"'
    w = csv.writer(resp)

    w.writerow([SYSPER_LABEL, "Last name", "First name", "Role", "Course", "Subject", "Start", "End", "Location"])
    for p in rows:
        t = p.training
        w.writerow(
            [
                person.sysper_id,
                person.last_name,
                person.first_name,
                p.role,
                t.course_name,
                (t.subject.name if t.subject_id else ""),
                localtime(t.start_at).strftime("%Y-%m-%d %H:%M"),
                localtime(t.end_at).strftime("%Y-%m-%d %H:%M"),
                t.location,
            ]
        )
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
        Participation.objects.filter(
            role="TRAINER",
            removed_at__isnull=True,
            training__start_at__gte=start,
            training__start_at__lt=end,
        )
        .select_related("person")
        .values("person__id", "person__sysper_id", "person__last_name", "person__first_name")
        .annotate(total_days=Sum("days"))
        .order_by("person__last_name", "person__first_name")
    )

    return render(
        request,
        "training/report_trainers.html",
        {
            "year": y,
            "month": m,
            "rows": qs,
        },
    )


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
        Participation.objects.filter(
            role="TRAINER",
            removed_at__isnull=True,
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
