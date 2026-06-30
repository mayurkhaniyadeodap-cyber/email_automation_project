from apps.scoping import OrgScopedViewSet

from .models import Category, Rule, SubTopic, Template
from .serializers import (
    CategoryListSerializer,
    CategorySerializer,
    RuleSerializer,
    SubTopicSerializer,
    TemplateSerializer,
)


class CategoryViewSet(OrgScopedViewSet):
    queryset = Category.objects.prefetch_related(
        "sub_topics__rules", "sub_topics__templates"
    )
    org_lookup = "brand__organization"
    brand_lookup = "brand"
    search_fields = ["code", "name"]

    def get_serializer_class(self):
        if self.action == "list":
            return CategoryListSerializer
        return CategorySerializer


class SubTopicViewSet(OrgScopedViewSet):
    serializer_class = SubTopicSerializer
    queryset = SubTopic.objects.select_related("category").prefetch_related(
        "rules", "templates"
    )
    org_lookup = "category__brand__organization"
    brand_lookup = "category__brand"
    search_fields = ["code", "name", "question"]


class RuleViewSet(OrgScopedViewSet):
    serializer_class = RuleSerializer
    queryset = Rule.objects.select_related("sub_topic__category")
    org_lookup = "sub_topic__category__brand__organization"
    brand_lookup = "sub_topic__category__brand"


class TemplateViewSet(OrgScopedViewSet):
    serializer_class = TemplateSerializer
    queryset = Template.objects.select_related("sub_topic__category")
    org_lookup = "sub_topic__category__brand__organization"
    brand_lookup = "sub_topic__category__brand"
    search_fields = ["name", "body"]
