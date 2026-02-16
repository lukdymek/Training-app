from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.timezone import now
from django.views.decorators.http import require_GET

from training.models import Participation, Person

from .common import staff_required


@login_required
@staff_required
def person_history(request, person_id):
    person = get_object_or_404(Person, pk=person_id)
    t = now()

    base_qs = (
        Participation.objects.filter(person=person, removed_at__isnull=True)
        .select_related("training", "training__subject")
    )

    completed = base_qs.filter(training__end_at__lt=t).order_by("-training__end_at", "-training__start_at")
    upcoming = base_qs.filter(training__start_at__gte=t).order_by("training__start_at")
    ongoing = base_qs.filter(training__start_at__lt=t, training__end_at__gte=t).order_by("training__end_at")

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
