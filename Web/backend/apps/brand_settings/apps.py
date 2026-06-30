try:
    from django.apps import AppConfig  # type: ignore[import]
except ImportError:
    class AppConfig:
        pass


class BrandSettingsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.brand_settings'
    label = 'brand_settings'
