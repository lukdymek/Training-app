from django.contrib import admin
from .models import Person, Training, Participation, Subject, TrainerSkill






@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    search_fields = ("name",)  # allows autocomplete + searching subjects


class TrainerSkillInline(admin.TabularInline):
    model = TrainerSkill
    extra = 0
    autocomplete_fields = ("subject",)


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = (
        "sysper_id",
        "last_name",
        "first_name",
        "email",
        "category",
        "current_deployment",
    )
    ordering = ("last_name", "first_name")
    search_fields = ("sysper_id", "last_name", "first_name", "email")
    inlines = [TrainerSkillInline]


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
