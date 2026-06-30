"""
The fixed complaint taxonomy (doc sections 4 & 5): 16 categories / 83 sub-topics
adapted from the Decision Tree Playbook. Defined PER BRAND so each brand can edit
its categories, sub-topic questions, THEN responses and Actions (doc sections 9 & 10).

The classifier prompt is built from these rows: each sub-topic's `question` plus its
rules' IF/THEN/Action table become the knowledge the model must match against.
"""

from django.db import models

from apps.common import TimestampedModel
from apps.organizations.models import Brand


class Category(TimestampedModel):
    """One of the 16 fixed categories, scoped to a brand."""

    brand = models.ForeignKey(
        Brand, on_delete=models.CASCADE, related_name="categories"
    )
    code = models.CharField(max_length=10, help_text="e.g. '3'")
    name = models.CharField(max_length=200)
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    # Every ticket in this category mandates a VIDEO (image-only insufficient) before
    # creation -- Defective / Missing / Wrong Item. Applies even when no specific
    # sub-topic is matched (sub_topic_ref is None).
    requires_video = models.BooleanField(default=False)

    class Meta:
        ordering = ["position", "code"]
        verbose_name_plural = "categories"
        constraints = [
            models.UniqueConstraint(
                fields=["brand", "code"], name="uniq_category_code_per_brand"
            )
        ]

    def __str__(self):
        return f"{self.code}. {self.name}"


class SubTopic(TimestampedModel):
    """A sub-topic under a category, e.g. '3.3 Shipment Lost or Damaged'."""

    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, related_name="sub_topics"
    )
    code = models.CharField(max_length=10, help_text="e.g. '3.3'")
    name = models.CharField(max_length=255)
    # The classifier "Skill" question this sub-topic answers.
    question = models.TextField(blank=True, default="")
    # Inputs the engine must have before it can answer (e.g. ["order_id"]).
    mandatory_inputs = models.JSONField(default=list, blank=True)
    # Always require photo/video evidence before a ticket is created (Smart Ticket
    # Management). Enforced regardless of the AI's per-email judgement.
    requires_evidence = models.BooleanField(default=False)
    # Stricter: require a VIDEO specifically (image-only is not sufficient) -- used
    # for Defective / Missing / Wrong Item before any ticket is created.
    requires_video = models.BooleanField(default=False)
    # Always route to a human regardless of confidence (doc section 6).
    is_sensitive = models.BooleanField(default=False)
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["position", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["category", "code"], name="uniq_subtopic_code_per_category"
            )
        ]

    def __str__(self):
        return f"{self.code} {self.name}"

    @property
    def brand(self):
        return self.category.brand


class Rule(TimestampedModel):
    """
    One IF/THEN/Action row of the decision engine (doc section 5).
    The Action maps to mail behavior (auto-send / draft / agent action).
    """

    ACTION_INFO_ONLY = "info_only"
    ACTION_AWAIT_EVIDENCE = "await_evidence"
    ACTION_CREATE_TICKET = "create_ticket"
    ACTION_UPDATE_SYSTEM = "update_system"
    ACTION_CONTINUE_CHECK = "continue_check"
    ACTION_TRIGGER_CRP = "trigger_cancellation_refund_pickup"
    ACTION_CHOICES = [
        (ACTION_INFO_ONLY, "Info only (auto-send)"),
        (ACTION_AWAIT_EVIDENCE, "Await evidence (auto-send template)"),
        (ACTION_CREATE_TICKET, "Create Ticket (draft only)"),
        (ACTION_UPDATE_SYSTEM, "Update in system (agent action)"),
        (ACTION_CONTINUE_CHECK, "Continue to next check"),
        (ACTION_TRIGGER_CRP, "Trigger cancellation / refund / pickup (agent action)"),
    ]

    sub_topic = models.ForeignKey(
        SubTopic, on_delete=models.CASCADE, related_name="rules"
    )
    # Human-readable IF condition, e.g. "Shipped AND EDD breached".
    condition = models.TextField(
        blank=True, default="", help_text="The IF condition (engine logic)"
    )
    then_response = models.TextField(
        blank=True, default="", help_text="The THEN response sent / drafted"
    )
    action = models.CharField(
        max_length=50, choices=ACTION_CHOICES, default=ACTION_CREATE_TICKET
    )
    position = models.PositiveIntegerField(
        default=0, help_text="Evaluation order within the sub-topic"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sub_topic", "position"]

    def __str__(self):
        return f"{self.sub_topic.code} #{self.position} -> {self.get_action_display()}"


class Template(TimestampedModel):
    """
    Auto-reply text per sub-topic so non-tech staff can reword it
    (doc section 10, Templates).
    """

    sub_topic = models.ForeignKey(
        SubTopic, on_delete=models.CASCADE, related_name="templates"
    )
    name = models.CharField(max_length=120, default="default")
    body = models.TextField(
        help_text="Supports placeholders like {order_id}, {tracking_url}, {edd}."
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sub_topic", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["sub_topic", "name"], name="uniq_template_name_per_subtopic"
            )
        ]

    def __str__(self):
        return f"{self.sub_topic.code} :: {self.name}"
