from django.contrib import admin

from .models import Category, Rule, SubTopic, Template


class SubTopicInline(admin.TabularInline):
    model = SubTopic
    extra = 0


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "brand", "position", "is_active")
    list_filter = ("brand", "is_active")
    search_fields = ("code", "name")
    inlines = [SubTopicInline]


class RuleInline(admin.TabularInline):
    model = Rule
    extra = 0


@admin.register(SubTopic)
class SubTopicAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "category", "is_sensitive", "is_active")
    list_filter = ("category__brand", "is_sensitive", "is_active")
    search_fields = ("code", "name", "question")
    inlines = [RuleInline]


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    list_display = ("sub_topic", "position", "action", "is_active")
    list_filter = ("action", "is_active")


@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    list_display = ("sub_topic", "name", "is_active")
    search_fields = ("name", "body")
