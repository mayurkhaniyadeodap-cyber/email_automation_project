from apps.scoping import OrgScopedViewSet

from .models import BlockListEntry, BrandSettings, SupportEmail
from .serializers import (
    BlockListEntrySerializer,
    BrandSettingsSerializer,
    SupportEmailSerializer,
)


class BrandSettingsViewSet(OrgScopedViewSet):
    serializer_class = BrandSettingsSerializer
    queryset = BrandSettings.objects.select_related("brand__organization")
    org_lookup = "brand__organization"
    brand_lookup = "brand"


class BlockListEntryViewSet(OrgScopedViewSet):
    serializer_class = BlockListEntrySerializer
    queryset = BlockListEntry.objects.select_related("brand__organization")
    org_lookup = "brand__organization"
    brand_lookup = "brand"
    search_fields = ["value", "note"]


class SupportEmailViewSet(OrgScopedViewSet):
    """The brand's support emails (the ONE fetched primary + sending-only aliases). Fully dynamic
    -- add/edit/delete/activate from Settings, no code changes ever needed."""

    serializer_class = SupportEmailSerializer
    queryset = SupportEmail.objects.select_related("brand__organization")
    org_lookup = "brand__organization"
    brand_lookup = "brand"
    search_fields = ["email", "display_name"]

    def perform_create(self, serializer):
        obj = serializer.save()
        self._enforce_single_primary(obj)

    def perform_update(self, serializer):
        obj = serializer.save()
        self._enforce_single_primary(obj)

    @staticmethod
    def _enforce_single_primary(obj):
        # At most ONE primary per brand (the single fetched inbox).
        if obj.is_primary:
            SupportEmail.objects.filter(brand=obj.brand, is_primary=True).exclude(
                pk=obj.pk).update(is_primary=False)
