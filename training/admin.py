
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
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
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
    list_display = ("sysper_id", "last_name", "first_name", "email", "category", "current_deployment")
    ordering = ("last_name", "first_name")
    search_fields = ("sysper_id", "last_name", "first_name", "email")

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
    




    @require_http_methods(["GET", "POST"])
    def import_people_excel(self, request):
        """
        Step 1 (POST with file): parse + validate + show preview
        Step 2 (POST confirm): import
        """
        if request.method == "GET":
            return render(request, "admin/training/import_people.html", {"form": PeopleImportForm()})

        # Confirm import
        if request.POST.get("confirm") == "1":
            import_rows = request.session.get("people_import_rows")
            if not import_rows:
                messages.error(request, "No pending import found. Please upload the file again.")
                return redirect("..")

            created = 0
            updated = 0
            for row in import_rows:
                sysper_id = row["sysper_id"]
                obj, was_created = Person.objects.update_or_create(
                    sysper_id=sysper_id,
                    defaults={
                        "first_name": row.get("first_name", ""),
                        "last_name": row.get("last_name", ""),
                        "email": row.get("email", ""),
                        "dob": row.get("dob"),
                        "gender": row.get("gender", ""),
                        "category": row.get("category", ""),
                        "current_deployment": row.get("current_deployment", ""),
                    },
                )
                created += 1 if was_created else 0
                updated += 0 if was_created else 1

            request.session.pop("people_import_rows", None)
            messages.success(request, f"Import complete. created={created}, updated={updated}")
            return redirect("..")

        # Upload + preview
        form = PeopleImportForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, "admin/training/import_people.html", {"form": form})

        f = form.cleaned_data["file"]

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

        errors = []
        warnings = []
        parsed = []
        seen_sysper = set()
        duplicates_in_file = set()

        if idx_sysper is None:
            errors.append(f"Could not find SYSPer column. Found headers: {headers}")

        # Parse rows
        if not errors:
            for row in ws.iter_rows(min_row=2, values_only=True):
                sysper_val = row[idx_sysper]
                if sysper_val is None or str(sysper_val).strip() == "":
                    continue

                try:
                    sysper_id = int(str(sysper_val).strip())
                except ValueError:
                    errors.append(f"Invalid SYSPer ID: {sysper_val}")
                    continue

                if sysper_id in seen_sysper:
                    duplicates_in_file.add(sysper_id)
                seen_sysper.add(sysper_id)

                first_name = (row[idx_first] if idx_first is not None else "") or ""
                last_name = (row[idx_last] if idx_last is not None else "") or ""
                email = (row[idx_email] if idx_email is not None else "") or ""
                dob = parse_date(row[idx_dob] if idx_dob is not None else None)
                gender = (row[idx_gender] if idx_gender is not None else "") or ""
                category = (row[idx_category] if idx_category is not None else "") or ""
                current_deployment = (row[idx_deploy] if idx_deploy is not None else "") or ""

                parsed.append({
                    "sysper_id": sysper_id,
                    "first_name": str(first_name).strip(),
                    "last_name": str(last_name).strip(),
                    "email": str(email).strip(),
                    "dob": dob,
                    "gender": str(gender).strip(),
                    "category": str(category).strip(),
                    "current_deployment": str(current_deployment).strip(),
                })

        if duplicates_in_file:
            warnings.append(f"Duplicate SYSPer IDs in file: {sorted(list(duplicates_in_file))[:10]} (showing up to 10)")

        # DB existence check
        existing = Person.objects.filter(sysper_id__in=[r["sysper_id"] for r in parsed]).values(
            "sysper_id", "first_name", "last_name"
        )
        existing_map = {e["sysper_id"]: e for e in existing}

        preview = []
        for r in parsed[:10]:
            ex = existing_map.get(r["sysper_id"])
            status = "NEW"
            mismatch = False
            if ex:
                status = "EXISTS"
                # warn if name differs (case-insensitive)
                if (ex["first_name"] or "").strip().lower() != (r["first_name"] or "").strip().lower() or \
                   (ex["last_name"] or "").strip().lower() != (r["last_name"] or "").strip().lower():
                    mismatch = True

            preview.append({"row": r, "status": status, "mismatch": mismatch})

        # If hard errors, show page again
        if errors:
            return render(request, "admin/training/import_people.html", {
                "form": PeopleImportForm(),
                "errors": errors,
                "warnings": warnings,
            })

        # Store parsed rows in session for confirmation step
        request.session["people_import_rows"] = parsed

        return render(request, "admin/training/import_people_preview.html", {
            "count": len(parsed),
            "preview": preview,
            "warnings": warnings,
            "headers": headers,
        })