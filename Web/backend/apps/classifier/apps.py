from django.apps import AppConfig  # type: ignore[reportMissingModuleSource]


class ClassifierConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.classifier"
    label = "classifier"
