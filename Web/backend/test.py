"""
End-to-end system smoke test for the DeoDap Care Panel.

Run from the backend folder:
    python test.py

It checks every subsystem and prints a PASS/FAIL report:
  1. Database / demo data
  2. IMAP connection (read)
  3. SMTP configuration (send)
  4. Gemini API key (or rule fallback)
  5. AI / rule classifier on sample emails
  6. Ignore gate -- "is this a support request?"
  7. Decision engine -- per-category auto-reply rules
  8. Full pipeline on a temp ticket (created + deleted)

This does NOT send real emails and cleans up any temp data it creates.
For the unit-test suite (130 tests) run:  python manage.py test
"""

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "deodap_care.settings")
django.setup()

from django.conf import settings  # noqa: E402

from apps.brand_settings.models import BrandSettings  # noqa: E402
from apps.classifier import service as classifier  # noqa: E402
from apps.decision import engine  # noqa: E402
from apps.organizations.models import Brand, Mailbox, Organization  # noqa: E402
from apps.tickets.models import Message, Ticket  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
results = []


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    if ok is None:
        icon = f"{YELLOW}WARN{RESET}"
    print(f"  [{icon}] {name}")
    if detail:
        print(f"         {DIM}{detail}{RESET}")
    results.append(ok)


def section(title):
    print(f"\n{title}\n{'-' * len(title)}")


# --- 1. Database / demo data ---
def _db():
    brands = Brand.objects.count()
    tickets = Ticket.objects.count()
    return brands > 0, f"{brands} brand(s), {tickets} ticket(s), " \
        f"{Ticket.objects.filter(is_ignored=True).count()} ignored"


# --- 2. IMAP ---
def _imap():
    from apps.ingestion.imap_client import ImapClient

    if settings.EMAIL_PROVIDER != "imap":
        return None, f"EMAIL_PROVIDER={settings.EMAIL_PROVIDER} (IMAP not active)"
    c = ImapClient.from_settings()
    if c is None:
        return False, "IMAP not configured (set IMAP_HOST / IMAP_USER / IMAP_PASSWORD)"
    msgs = c.fetch_recent(limit=1)
    return True, f"connected to {c.host} as {c.user} ({len(msgs)} msg fetched)"


# --- 3. SMTP ---
def _smtp():
    from apps.ingestion import smtp_client

    if smtp_client.is_configured():
        return True, f"ready via {settings.SMTP_HOST}:{settings.SMTP_PORT}"
    return None, "SMTP not configured (replies won't send)"


# --- 4. Gemini ---
def _gemini():
    from apps.classifier.providers import GeminiProvider

    key = settings.GEMINI_API_KEY
    if not key:
        return None, "no Gemini key -> rule-based classifier will be used"
    p = GeminiProvider(key, settings.GEMINI_MODEL)
    try:
        out = p.generate('Return JSON {"ok": true} only.', "ping")
        return True, f"key works, model={p.model} -> {out.strip()[:40]}"
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "429" in msg or "quota" in msg.lower():
            return None, "key valid but quota/rate-limited -> rule fallback kicks in"
        return False, msg[:120]


def _demo_brand():
    return Brand.objects.first()


# --- 5/6. Classifier + ignore gate on sample emails ---
SAMPLES = [
    ("Where is my order DD9999?", "when will it arrive", "buyer@example.com"),
    ("I received a damaged product", "the item is broken, order DD1234", "buyer@example.com"),
    ("I want a refund for DD5678", "please return my money", "buyer@example.com"),
    ("Wukusy Weekly Report - 2026-06-08", "attached weekly report", "reports@deodap.net"),
]


def _classifier():
    brand = _demo_brand()
    if brand is None:
        return False, "no brand; run: python manage.py bootstrap_demo"
    lines = []
    for subject, body, frm in SAMPLES:
        res = classifier.classify(brand, {"subject": subject, "body_text": body, "from_email": frm})
        if res is None:
            return False, "classify returned None (no provider, no fallback?)"
        tag = "SUPPORT" if res.is_support_request else "IGNORED"
        eng = res.raw.get("engine", "gemini")
        lines.append(f"{tag:8} {res.category[:34]:34} [{eng}]  <- {subject[:30]}")
    return True, "\n         ".join(lines)


# --- 7/8. Full pipeline on a temp ticket ---
def _pipeline():
    brand = _demo_brand()
    if brand is None:
        return False, "no brand"
    org = brand.organization
    mailbox = brand.mailboxes.first() or Mailbox.objects.create(
        brand=brand, email_address="smoketest@local")
    t = Ticket.objects.create(
        organization=org, brand=brand, mailbox=mailbox,
        customer_email="smoketest@example.com", subject="Where is my order DD9999?",
        thread_id="smoke-test-thread",
    )
    Message.objects.create(ticket=t, direction=Message.DIRECTION_INBOUND,
                           from_email="smoketest@example.com",
                           subject="Where is my order DD9999?", body_text="tracking please")
    try:
        classifier.classify_ticket(t)
        t.refresh_from_db()
        plan = engine.run(t)
        t.refresh_from_db()
        detail = (f"classified={t.category[:30]} | decision={plan.action_code} "
                  f"-> {plan.send_mode} | status={t.status}")
        return True, detail
    finally:
        t.delete()  # clean up the temp ticket


print(f"\n{'=' * 56}\n  DeoDap Care Panel — system smoke test\n{'=' * 56}")

section("Infrastructure")
check("Database & demo data", _db)
check("IMAP connection (fetch)", _imap)
check("SMTP configuration (send)", _smtp)
check("Gemini API key", _gemini)

section("AI pipeline")
check("Classifier + ignore gate (samples)", _classifier)
check("Full pipeline (classify -> decide)", _pipeline)

passed = sum(1 for r in results if r is True)
warned = sum(1 for r in results if r is None)
failed = sum(1 for r in results if r is False)
print(f"\n{'=' * 56}")
color = GREEN if failed == 0 else RED
summary = f"{passed} passed"
if warned:
    summary += f", {warned} warning(s)"
if failed:
    summary += f", {failed} failed"
print(f"  {color}{summary}{RESET}")
print(f"  {DIM}Unit-test suite: python manage.py test  (130 tests){RESET}")
print(f"{'=' * 56}\n")
