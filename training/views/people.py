from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.timezone import now
from django.views.decorators.http import require_GET

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


@login_required
def person_history(request, person_id):
    person = get_object_or_404(Person, pk=person_id)
    if not request.user.is_staff:
        user_email = (request.user.email or "").strip().lower()
        person_email = (person.email or "").strip().lower()
        if (not user_email) or (user_email != person_email):
            raise PermissionDenied
    t = now()

    base_qs = (
        Participation.objects.filter(person=person, removed_at__isnull=True)
        .select_related("training", "training__subject", "uof_assessment")
    )

    completed = _with_completion_display(
        list(base_qs.filter(training__end_at__lt=t).order_by("-training__end_at", "-training__start_at"))
    )
    upcoming = _with_completion_display(
        list(base_qs.filter(training__start_at__gte=t).order_by("training__start_at"))
    )
    ongoing = _with_completion_display(
        list(base_qs.filter(training__start_at__lt=t, training__end_at__gte=t).order_by("training__end_at"))
    )

    removed = (
        Participation.objects.filter(person=person, removed_at__isnull=False)
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
            "removed": removed,
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
                Q(sysper_id__startswith=int(q))
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
            )
        else:
            people = people.filter(Q(first_name__icontains=q) | Q(last_name__icontains=q))

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
        Q(sysper_id__icontains=q)
        | Q(last_name__icontains=q)
        | Q(first_name__icontains=q)
        | Q(email__icontains=q)
    ).order_by("last_name", "first_name")[:25]

    results = []

    for p in qs:
        results.append(
            {
                "id": p.id,
                "sysper_id": p.sysper_id,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "label": f"{p.sysper_id} — {p.last_name.upper()} {p.first_name}",
            }
        )

    return JsonResponse({"results": results})


@login_required
@staff_required
@require_GET
def trainer_search_api(request):
    q = (request.GET.get("q") or "").strip()

    if len(q) < 2:
        return JsonResponse({"results": []})

    base_qs = Person.objects.filter(
        Q(trainerskill__isnull=False) | Q(participation__role="TRAINER")
    ).distinct()

    qs = base_qs.filter(
        Q(sysper_id__icontains=q)
        | Q(last_name__icontains=q)
        | Q(first_name__icontains=q)
        | Q(email__icontains=q)
    ).order_by("last_name", "first_name")[:25]

    results = []
    for p in qs:
        results.append(
            {
                "id": p.id,
                "sysper_id": p.sysper_id,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "email": p.email,
                "label": f"{p.sysper_id} — {p.last_name.upper()} {p.first_name}",
            }
        )

    return JsonResponse({"results": results})
