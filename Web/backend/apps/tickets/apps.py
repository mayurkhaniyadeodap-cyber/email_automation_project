try:
    from django.apps import AppConfig  # type: ignore[import]
except (ImportError, ModuleNotFoundError):  # pragma: no cover - fallback for editor/static analysis when Django isn't installed
    class AppConfig:  # minimal fallback to avoid import errors in editors
        pass


class TicketsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tickets'
    label = 'tickets'
