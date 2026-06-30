from rest_framework import viewsets

from apps.scoping import OrgScopedViewSet

from .models import Brand, Mailbox, Organization
from .serializers import (
    BrandSerializer,
    MailboxSerializer,
    OrganizationSerializer,
)


class OrganizationViewSet(viewsets.ModelViewSet):
    """Top-level orgs the current user belongs to."""

    serializer_class = OrganizationSerializer
    queryset = Organization.objects.all()
    search_fields = ["name"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.is_superuser:
            return qs
        return qs.filter(members=self.request.user)

    def perform_create(self, serializer):
        org = serializer.save()
        org.members.add(self.request.user)


class BrandViewSet(OrgScopedViewSet):
    serializer_class = BrandSerializer
    queryset = Brand.objects.select_related("organization")
    org_lookup = "organization"
    brand_lookup = "pk"
    search_fields = ["name"]


class MailboxViewSet(OrgScopedViewSet):
    serializer_class = MailboxSerializer
    queryset = Mailbox.objects.select_related("brand", "brand__organization")
    org_lookup = "brand__organization"
    brand_lookup = "brand"
    search_fields = ["email_address"]
