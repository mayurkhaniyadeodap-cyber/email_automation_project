from rest_framework import serializers

from .models import Category, Rule, SubTopic, Template


class TemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Template
        fields = ["id", "sub_topic", "name", "body", "is_active",
                  "created_at", "updated_at"]


class RuleSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = Rule
        fields = [
            "id", "sub_topic", "condition", "then_response", "action",
            "action_display", "position", "is_active", "created_at", "updated_at",
        ]


class SubTopicSerializer(serializers.ModelSerializer):
    rules = RuleSerializer(many=True, read_only=True)
    templates = TemplateSerializer(many=True, read_only=True)

    class Meta:
        model = SubTopic
        fields = [
            "id", "category", "code", "name", "question", "mandatory_inputs",
            "requires_evidence", "requires_video", "is_sensitive", "position",
            "is_active", "rules", "templates", "created_at", "updated_at",
        ]


class CategorySerializer(serializers.ModelSerializer):
    sub_topics = SubTopicSerializer(many=True, read_only=True)
    sub_topic_count = serializers.IntegerField(
        source="sub_topics.count", read_only=True
    )

    class Meta:
        model = Category
        fields = [
            "id", "brand", "code", "name", "position", "is_active",
            "sub_topic_count", "sub_topics", "created_at", "updated_at",
        ]


class CategoryListSerializer(serializers.ModelSerializer):
    """Lightweight list view without nested sub-topics."""

    sub_topic_count = serializers.IntegerField(
        source="sub_topics.count", read_only=True
    )

    class Meta:
        model = Category
        fields = ["id", "brand", "code", "name", "position", "is_active",
                  "requires_video", "sub_topic_count"]
