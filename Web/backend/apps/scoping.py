"""
Org -> Brand scoping for the API.

The panel has two dropdowns at the top: select Organization -> select Brand.
Everything below (tickets, rules, settings) is scoped to the selected Brand
(doc section 9). This mixin enforces that on the server: a user only ever sees
data inside organizations they belong to, optionally narrowed by ?organization=
and ?brand= query params.
"""

from rest_framework import viewsets


class OrgScopedViewSet(viewsets.ModelViewSet):
    # Dotted path from this model to Organization, e.g. "organization"
    # for Brand, or "brand__organization" for brand-scoped resources.
    org_lookup = "organization"
    # Dotted path from this model to Brand for the ?brand= filter,
    # e.g. "brand" or "" if this model *is* a Brand (use "pk").
    brand_lookup = "brand"

    def get_user_orgs(self):
        return self.request.user.organizations.all()

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if not user.is_superuser:
            qs = qs.filter(**{f"{self.org_lookup}__in": self.get_user_orgs()})

        org = self.request.query_params.get("organization")
        if org:
            qs = qs.filter(**{self.org_lookup: org})

        brand = self.request.query_params.get("brand")
        if brand and self.brand_lookup:
            qs = qs.filter(**{self.brand_lookup: brand})

        return qs
