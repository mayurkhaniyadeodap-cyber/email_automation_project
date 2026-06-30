"""
Django settings for deodap_care project.

DeoDap Mail -> Care Panel Engine -- Phase 0: Foundations.
Backend: Django + Django REST Framework. DB: SQLite (dev). The data model is
JSON-first (JSONField everywhere) so a later swap to Postgres or MongoDB/Djongo
is a settings change, not a remodel.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(key, default=None):
    return os.environ.get(key, default)


SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    "django-insecure-b(cxqq-n_41^4%y+o-1hp1x_+jh#)*+7ht05k1ef$z0q4&@k!*",
)

DEBUG = env("DJANGO_DEBUG", "True").lower() in ("1", "true", "yes")

ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "*").split(",") if h]


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third-party
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    # local apps
    "apps.organizations",
    "apps.taxonomy",
    "apps.brand_settings",
    "apps.tickets",
    "apps.ingestion",
    "apps.classifier",
    "apps.decision",
    "apps.integrations",
    "apps.analytics",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "deodap_care.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "deodap_care.wsgi.application"


# Database -- SQLite for dev. To move to Postgres/MongoDB, swap this block only.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DJANGO_TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# Where `collectstatic` gathers static files for production (served by nginx). Harmless in dev.
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- Sub-path mounting (e.g. served under https://care.deodap.info/email_automation) ---
# FORCE_SCRIPT_NAME makes reverse()/redirects include the prefix; nginx strips the prefix
# from the proxied path so URL routing still matches /api, /admin. STATIC_URL stays
# relative ("static/") so Django auto-prepends the script name. Blank = mounted at root.
FORCE_SCRIPT_NAME = env("FORCE_SCRIPT_NAME", "") or None
if FORCE_SCRIPT_NAME:
    # Scope cookies to the sub-path and give them unique names so a SECOND Django app on
    # the same domain (e.g. care.deodap.info/support) can't clobber our session/CSRF.
    SESSION_COOKIE_PATH = FORCE_SCRIPT_NAME
    CSRF_COOKIE_PATH = FORCE_SCRIPT_NAME
    SESSION_COOKIE_NAME = "ea_sessionid"
    CSRF_COOKIE_NAME = "ea_csrftoken"

# Behind nginx doing TLS termination: trust its X-Forwarded-Proto so request.is_secure(),
# CSRF referer checks, and generated redirects use https.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Uploaded email attachments (images / videos / files).
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- Django REST Framework ---
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_FILTER_BACKENDS": [
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
}

# CORS -- React panel dev server.
CORS_ALLOWED_ORIGINS = [
    o
    for o in env(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:3000,http://localhost:5173",
    ).split(",")
    if o
]

# Origins trusted for unsafe (POST/PUT) requests -- Django 4+ requires this for the
# admin/login form behind an HTTPS reverse proxy. Comma-separated scheme://host values.
CSRF_TRUSTED_ORIGINS = [o for o in env("CSRF_TRUSTED_ORIGINS", "").split(",") if o]


# --- Email provider ---
# 'imap' (simplest: host/user/password, works with Zoho/Gmail/Outlook) or
# 'gmail' (Gmail API OAuth). IMAP needs no Google Cloud setup.
EMAIL_PROVIDER = env("EMAIL_PROVIDER", "imap")

# IMAP settings (used when EMAIL_PROVIDER=imap). For Gmail/Zoho use an APP PASSWORD.
#   Zoho global: imap.zoho.com | Zoho India: imap.zoho.in
#   Gmail: imap.gmail.com | Outlook: outlook.office365.com
IMAP_HOST = env("IMAP_HOST", "")
IMAP_PORT = int(env("IMAP_PORT", "993") or "993")
IMAP_USER = env("IMAP_USER", "")
# Gmail / Zoho APP PASSWORDS are 16 characters with NO spaces -- the provider UI shows them
# grouped ("xxxx xxxx xxxx xxxx") for readability only. A value pasted WITH the spaces makes
# BOTH SMTP and IMAP AUTH fail (535 Username and Password not accepted), so auto-replies
# silently never send. Strip whitespace so login works regardless of how it was pasted.
IMAP_PASSWORD = (env("IMAP_PASSWORD", "") or "").replace(" ", "")
IMAP_USE_SSL = env("IMAP_USE_SSL", "True").lower() in ("1", "true", "yes")
IMAP_FOLDER = env("IMAP_FOLDER", "INBOX")
IMAP_FETCH_LIMIT = int(env("IMAP_FETCH_LIMIT", "25") or "25")

# SMTP for SENDING replies (IMAP provider). Defaults derive from the IMAP host
# (imap.zoho.in -> smtp.zoho.in) and reuse the IMAP user/password.
SMTP_HOST = env("SMTP_HOST", "") or (IMAP_HOST.replace("imap.", "smtp.") if IMAP_HOST else "")
SMTP_PORT = int(env("SMTP_PORT", "465") or "465")
SMTP_USE_SSL = env("SMTP_USE_SSL", "True").lower() in ("1", "true", "yes")
# Address replies are sent FROM. Must be the authenticated account OR a verified
# "send as" alias on it, or the server rejects it (553 relay). Blank = IMAP_USER.
REPLY_FROM = env("REPLY_FROM", "")

# Company brochure PDF, auto-attached to the "Company Profile" inquiry auto-reply. Drop the
# real brochure at this path (or point COMPANY_BROCHURE_PATH at it); a placeholder ships in
# assets/ so the flow works out of the box.
COMPANY_BROCHURE_PATH = env("COMPANY_BROCHURE_PATH", str(BASE_DIR / "assets" / "company_profile.pdf"))
COMPANY_BROCHURE_FILENAME = env("COMPANY_BROCHURE_FILENAME", "company_profile.pdf")


# --- AI classifier (Phase 3) ---
# Optional GLOBAL Gemini key. When a brand hasn't pasted its own key in Settings,
# the classifier falls back to this one, so a single key in .env powers every brand.
GEMINI_API_KEY = env("GEMINI_API_KEY", "")
GEMINI_MODEL = env("GEMINI_MODEL", "")  # blank = gemini-2.5-flash
# Groq (OpenAI-compatible, e.g. llama-3.3-70b-versatile). Preferred over Gemini
# when set. Get a key at https://console.groq.com/keys
GROQ_API_KEY = env("GROQ_API_KEY", "")
GROQ_MODEL = env("GROQ_MODEL", "llama-3.3-70b-versatile")
OPENAI_API_KEY = env("OPENAI_API_KEY", "")
OPENAI_MODEL = env("OPENAI_MODEL", "")
# When the AI provider is missing or fails (e.g. Gemini quota), fall back to the
# deterministic keyword classifier so the pipeline still runs end-to-end.
CLASSIFIER_RULE_FALLBACK = env("CLASSIFIER_RULE_FALLBACK", "True").lower() in ("1", "true", "yes")
# Retry the AI call with exponential backoff on transient errors (429 / rate limit)
# before giving up. Delay = AI_RETRY_BASE_DELAY * 2**attempt seconds.
AI_MAX_RETRIES = int(env("AI_MAX_RETRIES", "3") or "3")
AI_RETRY_BASE_DELAY = float(env("AI_RETRY_BASE_DELAY", "2") or "2")

# Email the customer a "ticket created / updated" confirmation (Smart Ticket mgmt).
SEND_TICKET_CONFIRMATIONS = env("SEND_TICKET_CONFIRMATIONS", "True").lower() in ("1", "true", "yes")

# Auto-fetch new mail every N minutes while the server runs (0 = off / manual only).
AUTO_FETCH_MINUTES = int(env("AUTO_FETCH_MINUTES", "5") or "5")

# Waiting-state timers (Mail Flow v2.0 §8). The sweep job runs every
# WAITING_SWEEP_MINUTES and: sends a reminder (M7R) after REMINDER_HOURS, auto-closes
# (M7C) after AUTOCLOSE_HOURS, and a customer reply within REOPEN_DAYS reopens the case.
WAITING_SWEEP_MINUTES = int(env("WAITING_SWEEP_MINUTES", "30") or "30")
REMINDER_HOURS = int(env("REMINDER_HOURS", "24") or "24")
AUTOCLOSE_HOURS = int(env("AUTOCLOSE_HOURS", "72") or "72")
REOPEN_DAYS = int(env("REOPEN_DAYS", "7") or "7")

# Open the panel WITHOUT a login screen: auto-authenticate as AUTO_LOGIN_USER.
# Set AUTO_LOGIN=False in .env to restore the username/password login.
AUTO_LOGIN = env("AUTO_LOGIN", "True").lower() in ("1", "true", "yes")
AUTO_LOGIN_USER = env("AUTO_LOGIN_USER", "admin")

# INTERNAL recipients -- an email TO/Cc/Bcc any of these is an INTERNAL communication and must
# NEVER enter the customer-support pipeline (no ticket, auto-reply, escalation, verification...).
# It is routed to the separate Internal Communications inbox. Override via env (comma-separated).
# Reply-To for outbound customer mail = the address customer replies MUST land on (the inbox we
# poll). Leave blank to use the authenticated IMAP account (IMAP_USER) -- guaranteed deliverable.
# Set it to a branded address (e.g. care@deodap.com) ONLY if that address truly forwards to the
# polled inbox; otherwise replies (and their evidence) are lost.
REPLY_TO = env("REPLY_TO", "")

INTERNAL_RECIPIENTS = [
    a.strip().lower() for a in env(
        "INTERNAL_RECIPIENTS",
        "grievance.officer@deodap.com,nch-ca@gov.in,samir.rajani@gmail.com,"
        "samir@deodap.com,ceooffice@deodap.com").split(",") if a.strip()]

# --- Live integrations: global fallback from .env ---
# Per-brand BrandSettings.integrations still WINS; these env values are used for any brand
# that hasn't configured the integration in the DB. Lets you set Shopify once in .env.
SHOPIFY_SHOP = env("SHOPIFY_SHOP", "")               # e.g. deodap3.myshopify.com (no https://)
SHOPIFY_TOKEN = env("SHOPIFY_TOKEN", "")             # Admin API access token (shpat_...)
SHOPIFY_API_VERSION = env("SHOPIFY_API_VERSION", "2024-10")
# Courier / shipping portal (live AWB tracking). Optional.
SHIPPING_BASE_URL = env("SHIPPING_BASE_URL", "")
SHIPPING_API_KEY = env("SHIPPING_API_KEY", "")
# Public DeoDap shipping-portal tracking page. When Shopify gives an AWB but no tracking
# URL (and no courier integration), the "Track Live" link is built as <base>/<awb>.
SHIPPING_TRACKING_URL_BASE = env("SHIPPING_TRACKING_URL_BASE", "https://ship.deodap.in/tracking/")

# External DeoDap Care Panel ticket API (find-or-create open tickets by email + order).
CARE_PANEL_API_URL = env("CARE_PANEL_API_URL", "https://care.deodap.info/api/external/care-panel")
CARE_PANEL_API_KEY = env("CARE_PANEL_API_KEY", "")
# Care Panel SHIPMENT-FLOW API -- the PRIMARY tracking-status source (shipment.status /
# tracking.orderStatus, plus trackingUrl / awb / courier / edd). Same care.deodap.info auth.
CARE_PANEL_SHIPMENT_URL = env(
    "CARE_PANEL_SHIPMENT_URL", "https://care.deodap.info/api/external/shipping/shipment-flow")

# DeoDap Care Panel STORE API (care.deodap.in) -- creates the Gallabox ticket and
# returns the customer tracking link (https://care.deodap.in/t?id=...). Separate
# Laravel auth from the .info lookup above, so it has its own token.
CARE_PANEL_STORE_URL = env("CARE_PANEL_STORE_URL", "https://care.deodap.in/api/gallabox/ticket/store-json")
# Accept either name (CARE_PANEL_STORE_TOKEN or the shorter CARE_PANEL_TOKEN).
CARE_PANEL_STORE_TOKEN = env("CARE_PANEL_STORE_TOKEN", "") or env("CARE_PANEL_TOKEN", "")
# How to send the token: 'bearer' (Authorization: Bearer ..) or 'x-api-key'.
CARE_PANEL_STORE_AUTH = env("CARE_PANEL_STORE_AUTH", "bearer")

# Care Panel -> Mail Engine agent-reply webhook (Mail Flow §6 row 4). When an agent
# replies / changes status in the panel, it POSTs here and the Mail Engine mails the
# customer -- so the panel never touches the mailbox. Shared-secret auth (blank
# disables the auth check; set the SAME value on the Care Panel webhook config).
CARE_PANEL_WEBHOOK_TOKEN = env("CARE_PANEL_WEBHOOK_TOKEN", "")

# PUBLIC base URL of THIS Django app, where the /t tracking PORTAL is served (e.g.
# https://support.deodap.in). Every internal tracking link is built as
# PUBLIC_BASE_URL/t?id=<hash>. It MUST be the public URL of this app -- NOT localhost,
# an internal IP, or the external Care Panel (care.deodap.in / .info), none of which can
# resolve our hashes. If unset/invalid, NO tracking link is added (the confirmation
# email simply omits it) -- a broken link is never emailed.
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "")
# store-json required enum fields (verified live). source_id=3. Body schema: order_no /
# detail / issue_id / priority.
CARE_PANEL_STORE_SOURCE_ID = env("CARE_PANEL_STORE_SOURCE_ID", "3")

# --- Care Panel issue mapping (REAL fine-grained ids -- NOT our 1-16 category codes) ---
# The live Care Panel uses these specific issue ids/names (harvested from real tickets):
# e.g. id 3 = "Order Shown Delivered But Not Received", id 8 = "Damaged Or Bad Quality
# Items". So a damaged-item complaint MUST resolve to 8, never 3 -- the issue_id is chosen
# by the DETECTED ITEM SUB-TYPE, not by our broad taxonomy category. id -> Care Panel label
# (for logging + the find-or-create match).
# The REAL Gallabox / Care Panel issue catalog (provided by the brand). NOTE: there is NO id
# 6, 11 or 23 -- our old config wrongly used 11 ("Other Items"), which is why Website/App
# tickets showed "Other Items Related Issue".
CARE_PANEL_ISSUE_IDS = {
    "1": "Shipment Tracking", "2": "delivery delayed",
    "3": "order not received but shown as Delivered", "4": "Adress/Phoneno change",
    "5": "Order Cancellation request", "6": "payment issue", "7": "missing item",
    "8": "Damaged item", "9": "wrong item", "10": "item qty. issue", "12": "CyberFraud Report",
    "13": "order not received but shown as Out For Delivery", "14": "reschedule my delivery",
    "15": "RTO", "16": "defective / not working item", "17": "item not as per description",
    "18": "item quality issue", "19": "multiple issues", "20": "Account Related issues",
    "21": "Website/App Related issues", "22": "Offer/Discount Related issues",
}
# REAL Gallabox ids for the rolled-up groups (override via env if Gallabox changes them).
_WEBSITE_APP_ISSUE_ID = env("CARE_PANEL_WEBSITE_APP_ISSUE_ID", "21")  # Website/App Related issues
_ACCOUNT_ISSUE_ID = env("CARE_PANEL_ACCOUNT_ISSUE_ID", "20")         # Account Related issues
_OFFERS_ISSUE_ID = env("CARE_PANEL_OFFERS_ISSUE_ID", "22")           # Offer/Discount Related issues
# Catch-all (no specific issue detected) -> 19 "multiple issues" (a VALID Gallabox id).
_OTHER_ISSUE_ID = env("CARE_PANEL_OTHER_ISSUE_ID", "19")

# Detected issue-type name -> Care Panel issue id. The DELIVERED-ITEM sub-types map to their
# matching item issue -- Damaged->8, Defective->16, Missing->7, Wrong->9, Quantity->10,
# Quality->18 -- never to a not-received issue.
CARE_PANEL_ISSUE_MAP = {
    "Damaged Item": 8, "Defective Item": 16, "Missing Item": 7, "Wrong Item": 9,
    "Quantity Issue": 10, "Quality Issue": 18, "Other Issue": _OTHER_ISSUE_ID,
    "Delayed Delivery": 2, "Urgent Request": 2, "Reschedule Delivery": 14,
    "Undelivered Issue": 3, "Out For Delivery Issue": 13, "Cancelled Delivery": 15,
    # "Payment deducted but order not placed" = money taken with no order -> treated as a
    # CyberFraud Report (id 12) per the brand. (NOTE: there is NO valid id 6 in the real Care
    # Panel catalog, so the old "Payment Issue -> 6" mapping made the panel fall back to "Other
    # Delivery Related Issue".) Generic payment problems (double charge) keep the same target,
    # env-overridable. Refund has no dedicated issue -> generic 19 "multiple issues".
    "Shipment Tracking": 1,
    "Payment Fraud": env("CARE_PANEL_PAYMENT_NO_ORDER_ISSUE_ID", "12"),
    "Payment Issue": env("CARE_PANEL_PAYMENT_ISSUE_ID", "12"),
    "Refund Status": env("CARE_PANEL_REFUND_ISSUE_ID", _OTHER_ISSUE_ID),
    "Report Fraud": 12, "Update Address": 4, "Cancel Order": 5,
    # No dedicated Gallabox issue -> the generic "multiple issues" bucket (env-overridable).
    "Invoice Request": env("CARE_PANEL_INVOICE_ISSUE_ID", _OTHER_ISSUE_ID),
    "Franchise Inquiry": env("CARE_PANEL_FRANCHISE_ISSUE_ID", _OTHER_ISSUE_ID),
    "Dropshipping Inquiry": env("CARE_PANEL_DROPSHIP_ISSUE_ID", _OTHER_ISSUE_ID),
    "Company Profile Request": env("CARE_PANEL_COMPANY_ISSUE_ID", _OTHER_ISSUE_ID),
    # Website / App Related -> id 21 ("Website/App Related issues").
    "App Crashing / Not Loading": env("CARE_PANEL_APP_CRASH_ISSUE_ID", _WEBSITE_APP_ISSUE_ID),
    "Cart Not Saving Items": env("CARE_PANEL_CART_ISSUE_ID", _WEBSITE_APP_ISSUE_ID),
    "Checkout Page Not Load": env("CARE_PANEL_CHECKOUT_ISSUE_ID", _WEBSITE_APP_ISSUE_ID),
    "Saved Address Not Found": env("CARE_PANEL_SAVED_ADDRESS_ISSUE_ID", _WEBSITE_APP_ISSUE_ID),
    "Browser & Device Support": env("CARE_PANEL_BROWSER_ISSUE_ID", _WEBSITE_APP_ISSUE_ID),
    # Account Related -> id 20 ("Account Related issues"). All account sub-topics roll up here.
    "Password Reset Error": env("CARE_PANEL_PASSWORD_RESET_ISSUE_ID", _ACCOUNT_ISSUE_ID),
    "Update Phone / Email": env("CARE_PANEL_UPDATE_CONTACT_ISSUE_ID", _ACCOUNT_ISSUE_ID),
    "Delete Account": env("CARE_PANEL_DELETE_ACCOUNT_ISSUE_ID", _ACCOUNT_ISSUE_ID),
    "Data & Privacy Security": env("CARE_PANEL_DATA_PRIVACY_ISSUE_ID", _ACCOUNT_ISSUE_ID),
    "OTP / Notifications Not Received": env("CARE_PANEL_OTP_ISSUE_ID", _ACCOUNT_ISSUE_ID),
    "View Order History": env("CARE_PANEL_ORDER_HISTORY_ISSUE_ID", _ACCOUNT_ISSUE_ID),
    "Create New Account": env("CARE_PANEL_CREATE_ACCOUNT_ISSUE_ID", _ACCOUNT_ISSUE_ID),
    "Manage Saved Addresses": env("CARE_PANEL_MANAGE_ADDRESS_ISSUE_ID", _ACCOUNT_ISSUE_ID),
    # Offers -> id 22 ("Offer/Discount Related issues").
    "Ongoing Offers & Sales": env("CARE_PANEL_OFFERS_ISSUE_ID", _OFFERS_ISSUE_ID),
}
# Catch-all when no issue type is detected -> 19 "multiple issues" (a VALID Gallabox id).
CARE_PANEL_DEFAULT_ISSUE_ID = env("CARE_PANEL_DEFAULT_ISSUE_ID", _OTHER_ISSUE_ID)
# Numeric Website/App issue id the store call falls back to so the tracking link is ALWAYS
# created (and now shows the correct "Website/App Related issues" label).
CARE_PANEL_WEBSITE_APP_ISSUE_ID = _WEBSITE_APP_ISSUE_ID


# --- Gmail ingestion (Phase 1, doc section 2) ---
# One Google project / OAuth credential, one Cloud Pub/Sub watch per brand mailbox.
GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET", "")
# Pub/Sub topic to register with users.watch, e.g. projects/<proj>/topics/deodap-care-mail
GMAIL_PUBSUB_TOPIC = env("GMAIL_PUBSUB_TOPIC", "")
# Shared secret appended to the push subscription URL (?token=...) to authenticate it.
GMAIL_PUBSUB_TOKEN = env("GMAIL_PUBSUB_TOKEN", "")


# --- Logging: surface integration logs (Care Panel store/upload, lookups) ---
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "loggers": {
        "apps.integrations": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "apps.ingestion": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}


# --- Test safety: never touch real mail / AI servers during the test suite ---
import sys  # noqa: E402

if "test" in sys.argv:
    EMAIL_PROVIDER = "gmail"   # gmail path is a no-op without OAuth (no real send)
    IMAP_HOST = IMAP_USER = IMAP_PASSWORD = ""
    SMTP_HOST = ""
    # Tests use fake providers, never a live key (no real API calls / cost / quota).
    GEMINI_API_KEY = GROQ_API_KEY = OPENAI_API_KEY = ""
    # NEVER hit the live Shopify / courier APIs from tests -- the .env fallback would
    # otherwise build a real client for brands without a DB integration. Tests inject
    # fakes via build_clients monkeypatching.
    SHOPIFY_SHOP = SHOPIFY_TOKEN = ""
    SHIPPING_BASE_URL = SHIPPING_API_KEY = ""
    AI_RETRY_BASE_DELAY = 0    # no real backoff sleeps during tests
    CARE_PANEL_STORE_RETRY_BACKOFF = 0   # store-json retries don't sleep in tests
    # NEVER call the live DeoDap Care Panel from tests -- it would create real tickets.
    # Tokens are blanked (so the phone gate is off by default); tests that exercise the
    # phone gate override CARE_PANEL_STORE_TOKEN locally, and the localhost URLs keep
    # even those calls from leaving the machine.
    CARE_PANEL_API_KEY = CARE_PANEL_STORE_TOKEN = CARE_PANEL_WEBHOOK_TOKEN = ""
    CARE_PANEL_API_URL = "http://localhost:9/care-panel"
    CARE_PANEL_STORE_URL = "http://localhost:9/store-json"
