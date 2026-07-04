"""
Organization -> Brand -> Mailbox structure (doc section 9).

- Organization is the top level. A user first adds an Organization.
- An Organization has many Brands.
- Each Brand integrates its own Gmail mailbox, so that brand's customer-care
  mails only flow into that brand.
- A new mail is auto-assigned to the Brand based on which mailbox it arrived in.
"""

from django.conf import settings
from django.db import models
from django.utils.text import slugify

from apps.common import TimestampedModel


class Organization(TimestampedModel):
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="organizations",
        blank=True,
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class Brand(TimestampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="brands"
    )
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"], name="uniq_brand_slug_per_org"
            )
        ]

    def __str__(self):
        return f"{self.organization.name} / {self.name}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class Mailbox(TimestampedModel):
    """A Gmail mailbox integrated for a Brand (doc section 2)."""

    PROVIDER_GMAIL = "gmail"
    PROVIDER_CHOICES = [(PROVIDER_GMAIL, "Gmail API")]

    brand = models.ForeignKey(
        Brand, on_delete=models.CASCADE, related_name="mailboxes"
    )
    email_address = models.EmailField(unique=True)
    provider = models.CharField(
        max_length=20, choices=PROVIDER_CHOICES, default=PROVIDER_GMAIL
    )
    is_active = models.BooleanField(default=True)

    # Gmail push/watch state (doc section 2). Populated in Phase 1.
    gmail_history_id = models.CharField(max_length=64, blank=True, default="")
    watch_expiry = models.DateTimeField(null=True, blank=True)
    oauth_payload = models.JSONField(default=dict, blank=True)

    # IMAP incremental-fetch state: only mail with UID > imap_last_uid is fetched,
    # so old mail is never re-processed. UIDVALIDITY guards against mailbox resets.
    imap_last_uid = models.BigIntegerField(default=0)
    imap_uidvalidity = models.BigIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["email_address"]
        verbose_name_plural = "mailboxes"

    def __str__(self):
        return self.email_address


class UserProfile(TimestampedModel):
    """Role, display name, and login-lockout state for an auth user."""

    ROLE_ADMIN, ROLE_AGENT, ROLE_VIEWER = "admin", "agent", "viewer"
    ROLE_CHOICES = [(ROLE_ADMIN, "Admin"), (ROLE_AGENT, "Agent"), (ROLE_VIEWER, "Viewer")]

    # Nav each role may see; viewer is read-only.
    NAV = {
        ROLE_ADMIN: ["dashboard", "inbox", "tickets", "compose", "escalations",
                     "internal-communications", "settings"],
        ROLE_AGENT: ["inbox", "tickets", "compose"],
        ROLE_VIEWER: ["inbox", "tickets"],
    }
    MAX_FAILED_ATTEMPTS = 5

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    name = models.CharField(max_length=200, blank=True, default="")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_AGENT)
    failed_attempts = models.PositiveIntegerField(default=0)
    is_locked = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.user.get_username()} ({self.get_role_display()})"

    @property
    def effective_role(self):
        return self.ROLE_ADMIN if self.user.is_superuser else self.role

    @property
    def nav(self):
        return self.NAV.get(self.effective_role, self.NAV[self.ROLE_AGENT])

    @property
    def read_only(self):
        return self.effective_role == self.ROLE_VIEWER


class UserAuditLog(TimestampedModel):
    """Audit trail for user/account events (login, logout, member changes)."""

    USER_LOGIN = "USER_LOGIN"
    USER_LOGOUT = "USER_LOGOUT"
    USER_CREATED = "USER_CREATED"
    USER_DISABLED = "USER_DISABLED"
    USER_ENABLED = "USER_ENABLED"
    USER_UPDATED = "USER_UPDATED"
    USER_DELETED = "USER_DELETED"
    PASSWORD_RESET = "PASSWORD_RESET"
    LOGIN_FAILED = "LOGIN_FAILED"

    actor = models.CharField(max_length=150, blank=True, default="",
                             help_text="username who performed the action")
    event = models.CharField(max_length=40)
    target = models.CharField(max_length=150, blank=True, default="",
                              help_text="username affected by the action")
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event} {self.target} by {self.actor}"
