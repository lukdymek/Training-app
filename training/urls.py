from django.urls import path
from . import views
from django.conf.urls.static import static
from django.conf import settings

urlpatterns = [
    path("trainings/", views.training_list, name="training_list"),
    path("trainings/<int:pk>/", views.training_detail, name="training_detail"),
    path("trainings/<int:pk>/add-trainee/", views.add_trainee, name="add_trainee"),
    path("calendar/", views.calendar_view, name="calendar"),
    path("api/trainings/", views.trainings_json, name="trainings_json"),
    path("trainings/new/", views.training_create, name="training_create"),
    path("trainings/<int:pk>/add-trainer/", views.add_trainer, name="add_trainer"),
    path("trainers/", views.trainer_list, name="trainer_list"),
    path("trainers/<int:pk>/", views.trainer_detail, name="trainer_detail"),
    path("trainers/<int:pk>/add-skill/", views.add_trainer_skill, name="add_trainer_skill"),
    path("trainers/<int:pk>/remove-skill/<int:subject_id>/", views.remove_trainer_skill, name="remove_trainer_skill"),
    path("api/calendar-filters/", views.calendar_filters, name="calendar_filters"),
    path("reports/", views.reports_home, name="reports_home"),
    path("reports/person/", views.report_person, name="report_person"),
    path("reports/person/export/", views.report_person_export, name="report_person_export"),
    path("reports/trainers/", views.report_trainers, name="report_trainers"),
    path("reports/trainers/export/", views.report_trainers_export, name="report_trainers_export"),
    path("trainings/<int:pk>/trainer-days/<int:participation_id>/", views.update_trainer_days, name="update_trainer_days"),
    path("trainings/<int:pk>/edit/", views.training_edit, name="training_edit"),
    path("trainings/<int:pk>/delete/", views.training_delete, name="training_delete"),
    path("trainings/<int:pk>/people-search/", views.people_search, name="people_search"),
    path("trainings/<int:pk>/trainees/<int:person_id>/remove/", views.remove_trainee, name="remove_trainee"),
    path("trainings/<int:pk>/trainers/<int:person_id>/remove/", views.remove_trainer, name="remove_trainer"),
    path("trainings/<int:pk>/participation/<int:participation_id>/remove/", views.remove_participation, name="remove_participation"),
    path("trainings/<int:pk>/trainers/bulk-add/", views.add_trainers_bulk, name="add_trainers_bulk"),
    path("people/", views.people_list, name="people_list"),
    path("people/<int:person_id>/history/", views.person_history, name="person_history"),
    path("api/people/", views.people_search_api, name="people_search_api"),
    path("api/trainers/", views.trainer_search_api, name="trainer_search_api"),
    path("recurring-training/", views.recurring_training, name="recurring_training"),
    path("api/recurring-training/", views.recurring_training_api, name="recurring_training_api"),


]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)