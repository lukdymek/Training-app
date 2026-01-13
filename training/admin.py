from django.contrib import admin
from .models import Person, Training, Participation
from .models import Subject, TrainerSkill
admin.site.register(Subject)
admin.site.register(TrainerSkill)

@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ("sysper_id", "last_name", "first_name", "email", "category", "current_deployment")
    ordering = ("last_name", "first_name")
    search_fields = ("sysper_id", "last_name", "first_name", "email")

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
