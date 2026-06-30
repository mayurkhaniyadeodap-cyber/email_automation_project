from rest_framework import serializers

from .models import Brand, Mailbox, Organization


class MailboxSerializer(serializers.ModelSerializer):
    # Whether the mailbox has been authorized (OAuth tokens stored) -- never
    # expose the tokens themselves.
    connected = serializers.SerializerMethodField()

    class Meta:
        model = Mailbox
        fields = [
            "id", "brand", "email_address", "provider", "is_active", "connected",
            "gmail_history_id", "watch_expiry", "created_at", "updated_at",
        ]
        read_only_fields = ["gmail_history_id", "watch_expiry"]

    def get_connected(self, obj):
        return bool(obj.oauth_payload)


class BrandSerializer(serializers.ModelSerializer):
    mailbox_count = serializers.IntegerField(source="mailboxes.count", read_only=True)
    organization_name = serializers.CharField(
        source="organization.name", read_only=True
    )

    class Meta:
        model = Brand
        fields = [
            "id", "organization", "organization_name", "name", "slug",
            "is_active", "mailbox_count", "created_at", "updated_at",
        ]
        read_only_fields = ["slug"]


class OrganizationSerializer(serializers.ModelSerializer):
    brand_count = serializers.IntegerField(source="brands.count", read_only=True)

    class Meta:
        model = Organization
        fields = [
            "id", "name", "slug", "brand_count", "created_at", "updated_at",
        ]
        read_only_fields = ["slug"]
