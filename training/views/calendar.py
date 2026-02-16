"""Calendar and JSON feed endpoints."""

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import render
from django.utils.timezone import localtime

from ..models import Subject, Training


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


@login_required
def trainings_json(request):
    subject_id = request.GET.get("subject_id", "").strip()
    location = request.GET.get("location", "").strip()

    trainings = Training.objects.all()

    if subject_id.isdigit():
        trainings = trainings.filter(subject_id=int(subject_id))

    if location:
        trainings = trainings.filter(location__iexact=location)

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
            },
        })

    return JsonResponse(events, safe=False)


__all__ = ["calendar_view", "calendar_filters", "trainings_json"]
