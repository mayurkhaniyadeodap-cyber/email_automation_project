from django.contrib import admin

from .models import Brand, Mailbox, Organization


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "created_at")
    search_fields = ("name",)


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "is_active")
    list_filter = ("organization", "is_active")
    search_fields = ("name",)


@admin.register(Mailbox)
class MailboxAdmin(admin.ModelAdmin):
    list_display = ("email_address", "brand", "provider", "is_active")
    list_filter = ("provider", "is_active")
    search_fields = ("email_address",)
