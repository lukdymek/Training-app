
from .models import Person, Training, Participation, Subject, TrainerSkill
from django.contrib import admin, messages
from django.urls import path
from django.shortcuts import render, redirect
from django import forms
from django.db.models import Q
from django.views.decorators.http import require_http_methods
from django.utils.decorators import method_decorator
from openpyxl import load_workbook
from datetime import datetime, date
from django.db import transaction




@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    search_fields = ("name",)  # allows autocomplete + searching subjects


class TrainerSkillInline(admin.TabularInline):
    model = TrainerSkill
    extra = 0
    autocomplete_fields = ("subject",)





@admin.register(Training)
class TrainingAdmin(admin.ModelAdmin):
    list_display = ("course_name", "start_at", "end_at", "location", "capacity")
    search_fields = ("course_name", "location")
    ordering = ("-start_at",)


@admin.register(Participation)
class ParticipationAdmin(admin.ModelAdmin):
    list_display = ("person", "training", "role")
    search_fields = ("person__sysper_id", "person__last_name", "training__course_name")
    list_filter = ("role",)


@admin.register(TrainerSkill)
class TrainerSkillAdmin(admin.ModelAdmin):
    list_display = ("trainer", "subject")
    list_select_related = ("trainer", "subject")
    search_fields = (
        "subject__name",
        "trainer__first_name",
        "trainer__last_name",
        "trainer__sysper_id",
    )
    list_filter = ("subject",)
    search_help_text = "Search by subject name, trainer name, or SYSPER ID"
    autocomplete_fields = ("trainer", "subject")


def normalize_header(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "_")


def parse_date(value):
    if value is None or value == "":
        return None

    # IMPORTANT: datetime must be checked BEFORE date
    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    s = str(value).strip()

    # Handle ISO strings like 1900-01-01 or 1900-01-01T00:00:00
    try:
        # Python can parse both date and datetime ISO formats
        dt = datetime.fromisoformat(s)
        return dt.date()
    except Exception:
        pass

    try:
        return date.fromisoformat(s)
    except Exception:
        pass

    # Try common text formats
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass

    return None


class PeopleImportForm(forms.Form):
    file = forms.FileField(help_text="Upload Opera Evo Excel export (.xlsx)")
    replace_missing = forms.BooleanField(
        required=False,
        initial=True,
        help_text="Archive people not present in this file (recommended)."
    )


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ("sysper_id", "last_name", "first_name", "email", "category", "current_deployment", "is_active")
    ordering = ("last_name", "first_name")
    search_fields = ("sysper_id", "last_name", "first_name", "email")
    inlines = [TrainerSkillInline]

    # adds a button on the changelist page
    change_list_template = "admin/training/person_changelist.html"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "import-excel/",
                self.admin_site.admin_view(self.import_people_excel),
                name="training_person_import_excel",
            ),
        ]
        return custom + urls

    @transaction.atomic
    def import_people_excel(self, request):
        """
        Step 1 (POST without confirm):
            Upload Excel -> parse -> preview (store parsed rows in session)

        Step 2 (POST with confirm=1):
            Write to DB
            Optionally archive missing people (is_active=False)
        """

        # =====================================================
        # GET — show upload form
        # =====================================================
        if request.method == "GET":
            return render(
                request,
                "admin/training/import_people.html",
                {"form": PeopleImportForm()},
            )

        # =====================================================
        # POST — CONFIRM IMPORT
        # =====================================================
        if request.POST.get("confirm") == "1":
            rows = request.session.get("people_import_rows") or []
            archive_missing = request.session.get("people_import_archive_missing", True)

            if not rows:
                messages.error(request, "No pending import found. Please upload the file again.")
                return redirect("..")

            incoming_ids = set()
            created = 0
            updated = 0

            for r in rows:
                sysper_id = r.get("sysper_id")
                if not sysper_id:
                    continue

                incoming_ids.add(sysper_id)

                # DOB stored as ISO string in session → convert back to date
                dob = parse_date(r.get("dob"))

                obj, was_created = Person.objects.update_or_create(
                    sysper_id=sysper_id,
                    defaults={
                        "first_name": (r.get("first_name") or "").strip(),
                        "last_name": (r.get("last_name") or "").strip(),
                        "email": (r.get("email") or "").strip(),
                        "dob": dob,
                        "gender": (r.get("gender") or "").strip(),
                        "category": (r.get("category") or "").strip(),
                        "current_deployment": (r.get("current_deployment") or "").strip(),
                        "is_active": True,
                    },
                )

                if was_created:
                    created += 1
                else:
                    updated += 1

            archived = 0
            if archive_missing:
                archived = Person.objects.exclude(
                    sysper_id__in=incoming_ids
                ).update(is_active=False)

            # cleanup session
            request.session.pop("people_import_rows", None)
            request.session.pop("people_import_archive_missing", None)

            messages.success(
                request,
                f"Import complete. created={created}, updated={updated}, archived={archived}",
            )
            return redirect("..")

        # =====================================================
        # POST — UPLOAD / PREVIEW STEP
        # =====================================================

        form = PeopleImportForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(
                request,
                "admin/training/import_people.html",
                {"form": form},
            )

        f = form.cleaned_data["file"]
        archive_missing = form.cleaned_data.get("archive_missing", True)

        wb = load_workbook(filename=f, data_only=True)
        ws = wb.active

        headers = [normalize_header(c.value) for c in ws[1]]
        col = {h: i for i, h in enumerate(headers)}

        def pick(*names):
            for n in names:
                if n in col:
                    return col[n]
            return None

        idx_sysper = pick("sysper_id", "sysperid", "sysper", "id")
        idx_first = pick("first_name", "firstname", "first", "name", "given_name")
        idx_last = pick("last_name", "lastname", "last", "surname", "family_name")
        idx_email = pick("email", "email_address", "e-mail")
        idx_dob = pick("dob", "date_of_birth", "birth_date")
        idx_gender = pick("gender", "sex")
        idx_category = pick("category", "employee_category", "cat")
        idx_deploy = pick("current_deployment", "deployment", "current_assignment", "deployed")

        if idx_sysper is None:
            messages.error(
                request,
                f"Could not find SYSPer ID column. Headers detected: {headers}",
            )
            return render(
                request,
                "admin/training/import_people.html",
                {"form": PeopleImportForm()},
            )

        parsed = []
        incoming_ids = set()
        skipped = 0

        for row in ws.iter_rows(min_row=2, values_only=True):
            sysper_val = row[idx_sysper]
            if not sysper_val or str(sysper_val).strip() == "":
                skipped += 1
                continue

            try:
                sysper_id = int(str(sysper_val).strip())
            except ValueError:
                skipped += 1
                continue

            incoming_ids.add(sysper_id)

            first_name = (row[idx_first] if idx_first is not None else "") or ""
            last_name = (row[idx_last] if idx_last is not None else "") or ""
            email = (row[idx_email] if idx_email is not None else "") or ""

            # IMPORTANT: convert DOB to DATE, then to ISO STRING for session
            dob_date = parse_date(row[idx_dob] if idx_dob is not None else None)
            dob_iso = dob_date.isoformat() if dob_date else ""

            gender = (row[idx_gender] if idx_gender is not None else "") or ""
            category = (row[idx_category] if idx_category is not None else "") or ""
            current_deployment = (row[idx_deploy] if idx_deploy is not None else "") or ""

            parsed.append({
                "sysper_id": sysper_id,
                "first_name": str(first_name).strip(),
                "last_name": str(last_name).strip(),
                "email": str(email).strip(),
                "dob": dob_iso,  # SAFE for session (string)
                "gender": str(gender).strip(),
                "category": str(category).strip(),
                "current_deployment": str(current_deployment).strip(),
            })

        existing_ids = set(
            Person.objects.filter(sysper_id__in=incoming_ids)
            .values_list("sysper_id", flat=True)
        )

        will_update = sum(1 for r in parsed if r["sysper_id"] in existing_ids)
        will_create = len(parsed) - will_update
        will_archive = (
            Person.objects.exclude(sysper_id__in=incoming_ids).count()
            if archive_missing
            else 0
        )

        request.session["people_import_rows"] = parsed
        request.session["people_import_archive_missing"] = archive_missing

        return render(
            request,
            "admin/training/import_people_preview.html",
            {
                "count": len(parsed),
                "skipped": skipped,
                "preview": parsed[:10],
                "will_create": will_create,
                "will_update": will_update,
                "will_archive": will_archive,
                "archive_missing": archive_missing,
                "headers": headers,
            },
        )