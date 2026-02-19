import re
from datetime import date
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.staticfiles import finders
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from training.docx_utils import fill_bookmarks
from training.models import Participation, Training, UseOfForceStandard, UofAssessment, UofRating

from .common import staff_required, superuser_required



def seconds_to_mmss(seconds: int) -> str:
    s = int(seconds or 0)
    return f"{s//60:02d}:{s%60:02d}"


def mmss_to_seconds(value: str) -> int:
    v = (value or "").strip()
    if not v:
        return 0

    if "." in v and v.replace(".", "").isdigit():
        parts = v.split(".")
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = int(parts[1])
            if seconds >= 60:
                raise ValueError("Seconds must be < 60.")
            return minutes * 60 + seconds

    match = re.fullmatch(r"(\d{1,2}):(\d{2})", v)
    if not match:
        raise ValueError("Time must be MM:SS (e.g. 04:20).")

    minutes = int(match.group(1))
    seconds = int(match.group(2))
    if seconds >= 60:
        raise ValueError("Seconds must be < 60.")
    return minutes * 60 + seconds


@login_required
@superuser_required
def uof_standards(request):
    gender = (request.GET.get("gender") or UseOfForceStandard.GENDER_MALE).upper()
    if gender not in (UseOfForceStandard.GENDER_MALE, UseOfForceStandard.GENDER_FEMALE):
        gender = UseOfForceStandard.GENDER_MALE

    grouped = []
    for ex_key, ex_label in UseOfForceStandard.EXERCISE_CHOICES:
        rows = list(UseOfForceStandard.objects.filter(gender=gender, exercise=ex_key).order_by("age_sort"))

        if ex_key == UseOfForceStandard.EXERCISE_RUN:
            for row in rows:
                row.minimum_mmss = seconds_to_mmss(row.minimum)
                row.good_mmss = seconds_to_mmss(row.good)
                row.very_good_mmss = seconds_to_mmss(row.very_good)

        grouped.append(
            {
                "exercise_key": ex_key,
                "exercise_label": ex_label,
                "rows": rows,
            }
        )

    if request.method == "POST":
        updated = 0

        for block in grouped:
            is_run = block["exercise_key"] == UseOfForceStandard.EXERCISE_RUN

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

    return render(
        request,
        "training/uof_standards.html",
        {
            "grouped": grouped,
            "gender": gender,
        },
    )


def parse_mmss(value: str) -> int:
    v = (value or "").strip()
    if not v:
        return 0

    if "." in v and v.replace(".", "").isdigit():
        m, s = v.split(".", 1)
        return int(m) * 60 + int(s)

    match = re.fullmatch(r"(\d{1,2}):(\d{2})", v)
    if not match:
        raise ValueError("Run time must be MM:SS (e.g. 04:20).")

    mm = int(match.group(1))
    ss = int(match.group(2))
    if ss >= 60:
        raise ValueError("Seconds must be < 60.")
    return mm * 60 + ss


def age_on_date(dob: date, on_date: date) -> int:
    if not dob:
        return 0
    years = on_date.year - dob.year
    if (on_date.month, on_date.day) < (dob.month, dob.day):
        years -= 1
    return years


def age_group_key(age_years: int) -> str:
    if age_years <= 29:
        return "U29"
    if 30 <= age_years <= 34:
        return "30_34"
    if 35 <= age_years <= 39:
        return "35_39"
    if 40 <= age_years <= 44:
        return "40_44"
    if 45 <= age_years <= 49:
        return "45_49"
    if 50 <= age_years <= 54:
        return "50_54"
    if 55 <= age_years <= 59:
        return "55_59"
    return "60_PLUS"


def rating_for_reps(reps: int, std: UseOfForceStandard) -> str:
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
    age_group = age_group_key(age)

    gender = "M" if (person.gender or "").upper().startswith("M") else "F"

    def get_std(ex_key: str) -> UseOfForceStandard:
        return UseOfForceStandard.objects.get(gender=gender, exercise=ex_key, age_group=age_group)

    std_push = get_std("PUSHUPS")
    std_sit = get_std("SITUPS")
    std_run = get_std("RUN")

    assessment.pushups_rating = rating_for_reps(assessment.pushups, std_push)
    assessment.situps_rating = rating_for_reps(assessment.situps, std_sit)
    assessment.run_rating = rating_for_run(assessment.run_seconds, std_run)

    assessment.passed = (
        assessment.pushups_rating in [UofRating.MINIMUM, UofRating.GOOD, UofRating.VERY_GOOD]
        and assessment.situps_rating in [UofRating.MINIMUM, UofRating.GOOD, UofRating.VERY_GOOD]
        and assessment.run_rating in [UofRating.MINIMUM, UofRating.GOOD, UofRating.VERY_GOOD]
    )



@login_required
@require_POST
def uof_save_scores(request, training_id, participation_id):
    if not request.user.is_staff:
        return HttpResponseForbidden("You do not have permission.")

    participation = get_object_or_404(Participation, id=participation_id, training_id=training_id)

    assessment, _ = UofAssessment.objects.get_or_create(
        participation=participation,
        defaults={"tested_at": (participation.training.end_at.date() if participation.training.end_at else timezone.now().date())},
    )

    push = (request.POST.get("pushups") or "").strip()
    sit = (request.POST.get("situps") or "").strip()

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
        run_seconds = parse_mmss(run_time_raw)

    try:
        assessment.pushups = int(push) if push != "" else None
        assessment.situps = int(sit) if sit != "" else None
        assessment.run_seconds = run_seconds
        assessment.notes = notes

        compute_uof(assessment)
        assessment.save()

        has_scores = any(
            value is not None and value != ""
            for value in (assessment.pushups, assessment.situps, assessment.run_seconds)
        )
        participation.completion_status = ("PASS" if assessment.passed else "FAIL") if has_scores else ""
        participation.save(update_fields=["completion_status"])

        return JsonResponse(
            {
                "ok": True,
                "pushups_rating": assessment.pushups_rating,
                "situps_rating": assessment.situps_rating,
                "run_rating": assessment.run_rating,
                "passed": assessment.passed,
                "run_mmss": seconds_to_mmss(assessment.run_seconds) if assessment.run_seconds is not None else "",
            }
        )
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

    is_uof = training.subject is not None and (training.subject.name or "").strip().lower() == "use of force"
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

    trainees = Participation.objects.select_related("person").filter(training=training, role="TRAINEE").order_by(
        "person__last_name", "person__first_name"
    )

    for participation in trainees:
        assessment, _ = UofAssessment.objects.get_or_create(participation=participation)
        participation.assessment = assessment

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
    if not s:
        return ""
    s = str(s).strip()
    s = s.replace("_", " ").lower()
    return s[:1].upper() + s[1:]


@login_required
@staff_required
def uof_export_docx_one(request, training_id: int, participation_id: int):
    training = get_object_or_404(Training, pk=training_id)
    participation = get_object_or_404(
        Participation.objects.select_related("person", "training"),
        pk=participation_id,
        training=training,
    )
    assessment, _ = UofAssessment.objects.get_or_create(participation=participation)

    template_path = finders.find("Examination_card_template.docx")
    if not template_path:
        raise FileNotFoundError("Examination_card_template.docx not found in static files.")
    with open(template_path, "rb") as f:
        docx_bytes = f.read()

    person = participation.person
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
        "Gender": person.gender or "",
        "OfficerFitnessYesMark": "X",
        "OfficerFitnessNoMark": "",
        "PushScore": "" if assessment.pushups is None else str(assessment.pushups),
        "SitScore": "" if assessment.situps is None else str(assessment.situps),
        "RunScore": "" if assessment.run_seconds is None else seconds_to_mmss(assessment.run_seconds),
        "PushResult": _pretty_rating(getattr(assessment, "pushups_rating", "")),
        "SitResult": _pretty_rating(getattr(assessment, "situps_rating", "")),
        "RunResult": _pretty_rating(getattr(assessment, "run_rating", "")),
        "FinalAssessment": "Passed" if getattr(assessment, "passed", False) else "Failed",
        "Instructor1Name": training.uof_instructor_1 or "",
        "Instructor2Name": training.uof_instructor_2 or "",
        "ChairpersonName": training.uof_chairman or "",
    }

    docx_bytes = fill_bookmarks(docx_bytes, fields)

    filename = f"uof_{training.id}_{slugify(person.last_name)}_{slugify(person.first_name)}.docx"
    resp = HttpResponse(
        docx_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
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
        for participation in trainees:
            assessment, _ = UofAssessment.objects.get_or_create(participation=participation)

            template_path = finders.find("Examination_card_template.docx")
            if not template_path:
                raise FileNotFoundError("Examination_card_template.docx not found in static files.")
            with open(template_path, "rb") as f:
                docx_bytes = f.read()

            person = participation.person
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
                "Gender": person.gender or "",
                "OfficerFitnessYesMark": "X",
                "OfficerFitnessNoMark": "",
                "PushScore": "" if assessment.pushups is None else str(assessment.pushups),
                "SitScore": "" if assessment.situps is None else str(assessment.situps),
                "RunScore": "" if assessment.run_seconds is None else seconds_to_mmss(assessment.run_seconds),
                "PushResult": _pretty_rating(getattr(assessment, "pushups_rating", "")),
                "SitResult": _pretty_rating(getattr(assessment, "situps_rating", "")),
                "RunResult": _pretty_rating(getattr(assessment, "run_rating", "")),
                "FinalAssessment": "Passed" if getattr(assessment, "passed", False) else "Failed",
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


__all__ = [
    "uof_export_docx_all_zip",
    "uof_export_docx_one",
    "uof_results",
    "uof_save_scores",
    "uof_standards",
    "uof_update_meta",
]
