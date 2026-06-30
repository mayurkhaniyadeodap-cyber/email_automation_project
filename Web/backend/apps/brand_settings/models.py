"""
Per-brand Settings (doc section 10, "nothing hardcoded") and the Ignore/Block
gate lists (doc section 3). Everything configurable, per brand.
"""

from django.db import models  # type: ignore[import]

from apps.common import TimestampedModel
from apps.organizations.models import Brand


class BrandSettings(TimestampedModel):
    """One settings row per brand (AI provider, automation, confidence, holding reply)."""

    PROVIDER_GEMINI = "gemini"
    PROVIDER_CHATGPT = "chatgpt"
    PROVIDER_GROQ = "groq"
    PROVIDER_CHOICES = [
        (PROVIDER_GEMINI, "Gemini"),
        (PROVIDER_CHATGPT, "ChatGPT"),
        (PROVIDER_GROQ, "Groq (Llama)"),
    ]

    brand = models.OneToOneField(
        Brand, on_delete=models.CASCADE, related_name="settings"
    )

    # AI provider -- paste key, select model (doc section 10).
    ai_provider = models.CharField(
        max_length=20, choices=PROVIDER_CHOICES, default=PROVIDER_GEMINI
    )
    ai_api_key = models.CharField(max_length=255, blank=True, default="")
    ai_model = models.CharField(max_length=100, blank=True, default="")

    # Confidence threshold slider for when AI may auto-reply (doc section 6).
    confidence_threshold = models.FloatField(default=0.75)

    # Automation toggles per Action type: auto-send / draft / off (doc section 10).
    # e.g. {"info_only": "auto_send", "await_evidence": "auto_send",
    #       "create_ticket": "draft", "update_system": "off"}
    automation_toggles = models.JSONField(default=dict, blank=True)

    # "Await evidence" auto-sends by default; toggle to draft instead (doc section 5).
    await_evidence_autosend = models.BooleanField(default=True)

    # Predefined holding reply when AI can't handle it (doc section 6).
    holding_reply = models.TextField(
        default="Our team will review and get back to you shortly."
    )

    # Per-category SLA & priority targets (doc sections 10 & 12).
    # e.g. {"3": {"priority": "High", "first_response_mins": 120}}
    sla_config = models.JSONField(default=dict, blank=True)

    # Live-data integration credentials per brand (doc sections 1 & 5, Phase 5):
    # Shopify (order/EDD), Shipping Portal (tracking), GoKwik (payment). e.g.
    # {"shopify": {"shop": "x.myshopify.com", "token": "..."},
    #  "shipping": {"base_url": "...", "api_key": "..."},
    #  "gokwik": {"base_url": "...", "api_key": "..."}}
    integrations = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name_plural = "brand settings"

    def __str__(self):
        return f"Settings: {self.brand}"


class SupportEmail(TimestampedModel):
    """A support/sending email for the brand: the ONE fetched primary inbox plus any Gmail
    'Send mail as' ALIASES used only for sending replies. Fully dynamic (Settings -> Support
    Emails); nothing hardcoded. Any inbound email whose From/Return-Path/Sender matches an active
    SupportEmail is OUR OWN message and is NEVER imported (prevents the self-reply loop)."""

    brand = models.ForeignKey(
        Brand, on_delete=models.CASCADE, related_name="support_emails"
    )
    email = models.EmailField(help_text="Support inbox or 'send mail as' alias.")
    display_name = models.CharField(max_length=200, blank=True, default="")
    # The EMPLOYEE who owns this address. Replies sent from this alias are credited to this owner
    # in Employee Performance / reports, so all of a person's aliases count for the same employee.
    owner_name = models.CharField(max_length=200, blank=True, default="")
    is_primary = models.BooleanField(
        default=False, help_text="The ONE Gmail inbox actually fetched. Aliases are sending-only."
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-is_primary", "email"]
        # email is unique PER BRAND (the panel is multi-brand; the fetch scopes by brand).
        constraints = [
            models.UniqueConstraint(fields=["brand", "email"], name="uniq_brand_support_email"),
        ]

    def __str__(self):
        tag = "primary" if self.is_primary else "alias"
        return f"[{tag}] {self.email}"


class BlockListEntry(TimestampedModel):
    """
    One Ignore/Block gate rule (doc section 3). Nothing here is hardcoded --
    all lists live in Settings, per brand. A mail is IGNORED if ANY entry matches.
    """

    KIND_SENDER = "sender_email"
    KIND_DOMAIN = "sender_domain"
    KIND_MARKETING = "marketing"
    KIND_NOREPLY = "noreply"
    KIND_INTERNAL = "internal"
    KIND_SPAM = "spam"
    KIND_CHOICES = [
        (KIND_SENDER, "Block-list sender (exact email)"),
        (KIND_DOMAIN, "Block-list domain (e.g. *@newsletter.xyz)"),
        (KIND_MARKETING, "Marketing / bulk header"),
        (KIND_NOREPLY, "No-reply / automated pattern"),
        (KIND_INTERNAL, "Internal address"),
        (KIND_SPAM, "Spam / phishing pattern"),
    ]

    brand = models.ForeignKey(
        Brand, on_delete=models.CASCADE, related_name="block_list"
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    value = models.CharField(
        max_length=255,
        help_text="Email, domain pattern, or header/regex token to match.",
    )
    note = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["kind", "value"]
        verbose_name_plural = "block list entries"

    def __str__(self):
        return f"[{self.get_kind_display()}] {self.value}"
