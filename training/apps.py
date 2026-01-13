from django.apps import AppConfig


class TrainingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = 'training'

    def ready(self):
        from django.conf import settings
        from .constants import SYSPER_LABEL
        settings.SYSPER_LABEL = SYSPER_LABEL