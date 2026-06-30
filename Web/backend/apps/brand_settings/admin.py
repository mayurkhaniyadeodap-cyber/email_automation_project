from django.contrib import admin  # type: ignore[reportMissingModuleSource]

from .models import BlockListEntry, BrandSettings


@admin.register(BrandSettings)
class BrandSettingsAdmin(admin.ModelAdmin):
    list_display = ("brand", "ai_provider", "ai_model", "confidence_threshold")
    list_filter = ("ai_provider",)


@admin.register(BlockListEntry)
class BlockListEntryAdmin(admin.ModelAdmin):
    list_display = ("value", "kind", "brand", "is_active")
    list_filter = ("kind", "brand", "is_active")
    search_fields = ("value", "note")
