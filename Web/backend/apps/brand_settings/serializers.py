from rest_framework import serializers  # type: ignore[import]

from .models import BlockListEntry, BrandSettings, SupportEmail


class BrandSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = BrandSettings
        fields = [
            "id", "brand", "ai_provider", "ai_api_key", "ai_key_set", "ai_model",
            "confidence_threshold", "automation_toggles", "await_evidence_autosend",
            "holding_reply", "sla_config", "integrations", "created_at", "updated_at",
        ]
        extra_kwargs = {"ai_api_key": {"write_only": True, "required": False}}

    # The key itself is write-only; expose only whether one is configured.
    ai_key_set = serializers.SerializerMethodField()

    def get_ai_key_set(self, obj):
        return bool(obj.ai_api_key)


class BlockListEntrySerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = BlockListEntry
        fields = [
            "id", "brand", "kind", "kind_display", "value", "note",
            "is_active", "created_at", "updated_at",
        ]


class SupportEmailSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportEmail
        fields = [
            "id", "brand", "email", "display_name", "owner_name", "is_primary", "is_active",
            "created_at", "updated_at",
        ]

    def validate_email(self, value):
        return (value or "").strip().lower()
