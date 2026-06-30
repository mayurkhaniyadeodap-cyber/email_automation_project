"""
Classifier service (doc section 4): send a mail to the brand's AI provider, get back
{category, sub_topic, confidence, extracted, language, sentiment}, map it onto the
FIXED taxonomy, and write the result to the ticket.

The provider is injectable (`build_provider`) so the whole flow is unit-testable
offline with a fake. Mapping is strict: the model's free-text category/sub_topic is
resolved back to real Category/SubTopic rows by their leading code, and anything
that doesn't resolve becomes the `Uncategorized` fallback that routes to an agent.
"""

import json
import logging
import re
from dataclasses import dataclass, field

from django.utils import timezone

from apps.brand_settings.models import BrandSettings
from apps.taxonomy.models import Category, SubTopic
from apps.tickets.models import AuditLogEntry, Message, Ticket

from . import skills

logger = logging.getLogger(__name__)

_CODE_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)")


@dataclass
class ClassificationResult:
    category: str = skills.UNCATEGORIZED
    sub_topic: str = ""
    confidence: float = 0.0
    extracted: dict = field(default_factory=dict)
    language: str = ""
    sentiment: str = ""
    is_support_request: bool = True
    issue_summary: str = ""
    requires_evidence: bool = False
    requires_agent: bool = False
    action: str = ""
    category_ref: Category | None = None
    sub_topic_ref: SubTopic | None = None
    raw: dict = field(default_factory=dict)

    @property
    def is_uncategorized(self):
        return self.sub_topic_ref is None


def build_provider(settings):
    """Provider factory; patched in tests with a fake. Lazy-imports the SDK adapters."""
    from .providers import get_provider

    return get_provider(settings)


def _settings_for(brand):
    try:
        return brand.settings
    except BrandSettings.DoesNotExist:
        return None


def _leading_code(text):
    m = _CODE_RE.match(text or "")
    return m.group(1) if m else ""


def _parse_json(text):
    """Pull the JSON object out of a model response (tolerates ``` fences / prose)."""
    if not text:
        raise ValueError("empty model response")
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        # drop a leading 'json' language tag if present
        if text[:4].lower() == "json":
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _clamp_confidence(value):
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, c))


def _map_taxonomy(brand, category_str, sub_topic_str):
    """Resolve free-text category/sub_topic back to real rows by leading code."""
    sub_code = _leading_code(sub_topic_str)
    if sub_code:
        sub = (
            SubTopic.objects.filter(category__brand=brand, code=sub_code)
            .select_related("category")
            .first()
        )
        if sub:
            return sub.category, sub

    cat_code = _leading_code(category_str)
    if cat_code:
        cat = Category.objects.filter(brand=brand, code=cat_code).first()
        if cat:
            return cat, None

    return None, None


def _normalize(brand, data):
    category_str = data.get("category", "") or ""
    sub_topic_str = data.get("sub_topic", "") or ""
    cat_ref, sub_ref = _map_taxonomy(brand, category_str, sub_topic_str)

    if sub_ref is not None:
        category_str = f"{sub_ref.category.code}. {sub_ref.category.name}"
        sub_topic_str = f"{sub_ref.code} {sub_ref.name}"
    elif cat_ref is not None:
        category_str = f"{cat_ref.code}. {cat_ref.name}"
        sub_topic_str = ""
    else:
        category_str = skills.UNCATEGORIZED
        sub_topic_str = ""

    extracted = data.get("extracted")
    if not isinstance(extracted, dict):
        extracted = {}

    return ClassificationResult(
        category=category_str,
        sub_topic=sub_topic_str,
        confidence=_clamp_confidence(data.get("confidence")),
        extracted=extracted,
        language=str(data.get("language", "") or ""),
        sentiment=str(data.get("sentiment", "") or ""),
        is_support_request=bool(data.get("is_support_request", True)),
        issue_summary=str(data.get("issue_summary", "") or ""),
        requires_evidence=bool(data.get("requires_evidence", False)),
        requires_agent=bool(data.get("requires_agent", False)),
        action=str(data.get("action", "") or ""),
        category_ref=cat_ref,
        sub_topic_ref=sub_ref,
        raw=data,
    )


def _rule_fallback_enabled():
    from django.conf import settings as dj

    return getattr(dj, "CLASSIFIER_RULE_FALLBACK", True)


def _rule_classify(brand, message):
    from . import rule_classifier

    data = rule_classifier.build_data(brand, message)
    result = _normalize(brand, data)
    result.raw = {"engine": "rules", **data}
    return result


def _is_retryable(exc):
    """429 / rate-limit / transient server errors are worth retrying."""
    s = str(exc).lower()
    return any(t in s for t in ("429", "quota", "rate limit", "rate-limit",
                                "resourceexhausted", "503", "unavailable", "timeout"))


def _generate_with_retry(provider, system, user):
    """Call the AI provider with exponential backoff on transient errors.
    Raises the last exception if all attempts fail."""
    import time

    from django.conf import settings as dj

    retries = getattr(dj, "AI_MAX_RETRIES", 3)
    base = getattr(dj, "AI_RETRY_BASE_DELAY", 2.0)
    last = None
    for attempt in range(retries + 1):
        try:
            return provider.generate(system, user)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= retries or not _is_retryable(exc):
                raise
            delay = base * (2 ** attempt)
            logger.warning("AI call failed (%s); retry %d/%d in %.0fs",
                           str(exc)[:80], attempt + 1, retries, delay)
            time.sleep(delay)
    raise last


def same_issue(brand, summary_a, summary_b, provider=None):
    """Ask the AI whether two issue summaries are the SAME ongoing issue.

    Returns (same_issue: bool, confidence: float), or None when no provider is
    configured / the call fails (caller falls back to a heuristic).
    """
    provider = provider or build_provider(_settings_for(brand))
    if provider is None or not summary_a or not summary_b:
        return None
    system = (
        "You compare two customer-support issues from the same customer. Return STRICT "
        'JSON ONLY: {"same_issue": <true|false>, "confidence": <0..1>}. same_issue is '
        "true ONLY if they are the same ongoing problem (same order / request / topic), "
        "not merely the same category."
    )
    user = f"Issue A: {summary_a}\nIssue B: {summary_b}"
    try:
        data = _parse_json(_generate_with_retry(provider, system, user))
        return bool(data.get("same_issue")), _clamp_confidence(data.get("confidence"))
    except Exception:  # noqa: BLE001
        return None


def ai_generate(brand, message, provider):
    """Run the AI classifier with retry. Returns a ClassificationResult.
    Raises on failure (after retries) -- callers decide whether to fall back."""
    system, user = skills.build_prompt(brand, message)
    text = _generate_with_retry(provider, system, user)
    return _normalize(brand, _parse_json(text))


# --------------------------------------------------------------------------- #
# Website / App override (HIGHEST priority). A website or mobile-app fault must NEVER be
# classified as a delivery / tracking / item issue -- these phrases force Category 15
# (Website / App Related) + the matching sub-topic, overriding the AI or rule classifier.
# --------------------------------------------------------------------------- #
_WEBSITE_APP_TRIGGERS = (
    # App faults
    "app crash", "app crashing", "app crashes", "app crashed", "application crash",
    "application error", "app error", "app not open", "app not opening", "app won't open",
    "app wont open", "app not loading", "app not working", "app keeps crashing", "app hang",
    "app freezing", "app freeze", "app keeps closing", "app closing",
    # Website faults
    "website crash", "website error", "website not opening", "website not loading",
    "website not working", "site not opening", "site not loading", "web not opening",
    "page not loading", "page not opening", "page not load",
    # Checkout
    "checkout page not load", "checkout page not loading", "checkout not loading",
    "checkout not working", "checkout page", "checkout error",
    # Cart
    "cart not saving", "cart not updating", "cart empties", "cart not working",
    # Saved address
    "saved address missing", "saved address not found", "address not saving", "saved address",
    # Browser / device
    "browser issue", "browser compatibility", "browser & device", "browser and device",
    "device compatibility", "incompatible browser", "browser", "not supported",
    "unsupported", "device support",
)

# Sub-topic resolution: most-specific first; the generic app/website crash is the fallback.
_WEBSITE_APP_SUBTOPICS = (
    ("Cart Not Saving Items", ("cart not saving", "cart not updating", "cart empties",
                               "cart not working")),
    ("Saved Address Not Found", ("saved address", "address not saving", "address not found",
                                 "address missing")),
    ("Checkout Page Not Load", ("checkout",)),
    ("Browser & Device Support", ("browser", "device compatibility", "incompatible")),
    ("App Crashing / Not Loading", ()),     # fallback (empty keyword set)
)


def _website_app_subtopic(blob):
    for name, kws in _WEBSITE_APP_SUBTOPICS:
        if not kws or any(k in blob for k in kws):
            return name
    return "App Crashing / Not Loading"


def _correct_website_app_category(brand, message, result):
    """Force Category 15 (Website / App Related) for clear app/website fault keywords, so the
    AI can never file an "app crash" under a Delivery / Tracking / Item issue. Returns
    (result, fired)."""
    if result is None:
        return result, False
    blob = (f"{message.get('subject','') or ''} "
            f"{message.get('body_text') or message.get('snippet') or ''}").lower()
    if not any(t in blob for t in _WEBSITE_APP_TRIGGERS):
        return result, False

    from apps.taxonomy.models import Category, SubTopic

    was_topic, was_sub = result.category or "-", result.sub_topic or "-"
    sub_name = _website_app_subtopic(blob)
    cat = (result.category_ref if (result.category_ref and result.category_ref.code == "15")
           else Category.objects.filter(brand=brand, code="15").first())
    if cat is not None:
        result.category_ref = cat
        result.category = f"{cat.code}. {cat.name}"
        sub = (SubTopic.objects.filter(category=cat, name__iexact=sub_name).first()
               or SubTopic.objects.filter(
                   category=cat, name__icontains=sub_name.split("/")[0].strip()).first())
        if sub is not None:
            result.sub_topic_ref = sub
            result.sub_topic = f"{sub.code} {sub.name}"
        else:                                   # cat 15 has no seeded sub-topics -> use the name
            result.sub_topic_ref = None
            result.sub_topic = sub_name
    else:                                       # no cat-15 in taxonomy -> still stamp the labels
        result.category = "15. Website / App Related"
        result.sub_topic = sub_name
    result.requires_evidence = False            # an app/website fault is never a delivered item
    logger.info("CLASSIFICATION_TOPIC=%s", "Website / App Related")
    logger.info("CLASSIFICATION_SUBTOPIC=%s", sub_name)
    logger.info("CLASSIFICATION-TOPIC=%s", result.category)
    logger.info("CLASSIFICATION-SUBTOPIC=%s", result.sub_topic)
    logger.info("CLASSIFICATION-REASON=website/app keyword override (was topic=%r sub=%r)",
                was_topic, was_sub)
    return result, True


def _correct_offers_category(brand, message, result):
    """Force Category 10 (Ongoing Offers & Sales) for offer / discount / coupon / sale / promo /
    deal keywords, so the AI can never file an offers inquiry under a Delivery / Tracking /
    Missing-Item issue. Sub-topic: 'Discount Issue' (a coupon/discount NOT working) else 'Offer
    Inquiry'. Returns (result, fired)."""
    from apps.decision import policy
    from apps.taxonomy.models import Category, SubTopic

    if result is None:
        return result, False
    blob = (f"{message.get('subject','') or ''} "
            f"{message.get('body_text') or message.get('snippet') or ''}")
    kind = policy.offers_flow(blob)
    if not kind:
        return result, False

    was_topic, was_sub = result.category or "-", result.sub_topic or "-"
    sub_name = "Discount Issue" if kind == "offers_problem" else "Offer Inquiry"
    cat = (result.category_ref if (result.category_ref and result.category_ref.code == "10")
           else Category.objects.filter(brand=brand, code="10").first())
    if cat is not None:
        result.category_ref = cat
        result.category = f"{cat.code}. {cat.name}"
        sub = SubTopic.objects.filter(category=cat, name__iexact=sub_name).first()
        if sub is not None:
            result.sub_topic_ref = sub
            result.sub_topic = f"{sub.code} {sub.name}"
        else:
            result.sub_topic_ref = None
            result.sub_topic = sub_name
    else:
        result.category = "10. Ongoing Offers & Sales"
        result.sub_topic = sub_name
    result.requires_evidence = False
    logger.info("CLASSIFICATION-TOPIC=Ongoing Offers & Sales")
    logger.info("CLASSIFICATION-SUBTOPIC=%s", result.sub_topic)
    logger.info("CLASSIFICATION-REASON=offer/discount keyword override (was topic=%r sub=%r)",
                was_topic, was_sub)
    return result, True


def _correct_payment_category(brand, message, result):
    """Force Category 8 (Payment & Invoice) -> 'Payment Deducted But Order Not Placed' for any
    'payment deducted/debited but order not placed' phrasing, so the AI can never file it under a
    Delivery / Tracking / Missing-Item issue. Requires a PAYMENT SCREENSHOT (photo, not video).
    Returns (result, fired)."""
    from apps.decision import policy
    from apps.taxonomy.models import Category, SubTopic

    if result is None:
        return result, False
    blob = (f"{message.get('subject','') or ''} "
            f"{message.get('body_text') or message.get('snippet') or ''}")
    if not policy.payment_no_order(blob):
        return result, False

    was_topic, was_sub = result.category or "-", result.sub_topic or "-"
    sub_name = "Payment Deducted But Order Not Placed"
    cat = (result.category_ref if (result.category_ref and result.category_ref.code == "8")
           else Category.objects.filter(brand=brand, code="8").first())
    if cat is not None:
        result.category_ref = cat
        result.category = f"{cat.code}. {cat.name}"
        sub = SubTopic.objects.filter(category=cat, name__iexact=sub_name).first()
        if sub is not None:
            result.sub_topic_ref = sub
            result.sub_topic = f"{sub.code} {sub.name}"
        else:
            result.sub_topic_ref = None
            result.sub_topic = sub_name
    else:
        result.category = "8. Payment & Invoice"
        result.sub_topic = sub_name
    result.requires_evidence = True             # Payment Screenshot (photo) is mandatory
    logger.info("CLASSIFICATION_TOPIC=Payment Issue")
    logger.info("CLASSIFICATION_SUBTOPIC=%s", sub_name)
    logger.info("CLASSIFICATION-TOPIC=%s", result.category)
    logger.info("CLASSIFICATION-SUBTOPIC=%s", result.sub_topic)
    logger.info("CLASSIFICATION-REASON=payment-deducted/no-order override (was topic=%r sub=%r)",
                was_topic, was_sub)
    return result, True


def _post_correct(brand, message, result):
    """Deterministic post-classification fixes (in priority order). Payment-deducted/no-order
    wins first (never a delivery/item case), then Website/App, then Offers, else the
    Delivered-Item sub-type correction."""
    result, fired = _correct_payment_category(brand, message, result)
    if fired:
        return result
    result, fired = _correct_website_app_category(brand, message, result)
    if fired:
        return result
    result, fired = _correct_offers_category(brand, message, result)
    if fired:
        return result
    return _correct_delivered_item_subtype(brand, message, result)


def _delivered_item_categories():
    # Codes whose sub-type is a Delivered-Item condition (Delivery Issues / Returns).
    return ("3", "7")


def _correct_delivered_item_subtype(brand, message, result):
    """Deterministically fix the Delivered-Item SUB-TYPE from keywords, so the AI can never
    label a damaged-item email "Missing Item" (or vice-versa). Logs the raw AI output and the
    corrected category. Only fires for clear item-condition keywords on a post-delivery /
    returns case -- a tracking/payment email is never reclassified into a damage sub-type."""
    from apps.ingestion import evidence
    from apps.taxonomy.models import SubTopic

    if result is None:
        return result
    raw = result.raw or {}
    logger.info("CLASSIFIER-RAW category=%r sub_topic=%r confidence=%s",
                raw.get("category", result.category), raw.get("sub_topic", result.sub_topic),
                raw.get("confidence"))

    text = f"{message.get('subject','') or ''} {message.get('body_text') or message.get('snippet') or ''}"
    if evidence.is_cancellation(text):
        logger.info("CLASSIFIER-CATEGORY category=%r sub_topic=%r (cancellation -> no subtype "
                    "correction)", result.category, result.sub_topic)
        return result
    subtype = evidence.delivered_item_subtype(text)
    cat_code = (result.category or "").split(".")[0].strip()
    raw_sub = (result.sub_topic or "")
    is_delivered_item = (cat_code in _delivered_item_categories() or result.requires_evidence
                         or any(s in raw_sub for s in
                                ("Damaged", "Defective", "Missing", "Wrong", "Quantity")))

    if subtype and is_delivered_item and subtype not in raw_sub:
        # Keep the AI's CATEGORY; re-map only the sub-topic. Prefer a real SubTopic ref whose
        # name matches the corrected sub-type (exact name beats a loose contains, and the AI's
        # category beats brand-wide); else fall back to the plain sub-type string.
        cat = result.category_ref
        sub = None
        for flt in (
            dict(category=cat, name__iexact=subtype) if cat else None,
            dict(category=cat, name__icontains=subtype) if cat else None,
            dict(category__brand=brand, name__iexact=subtype),
            dict(category__brand=brand, name__icontains=subtype),
        ):
            if flt is None:
                continue
            sub = SubTopic.objects.filter(**flt).first()
            if sub is not None:
                break
        old = result.sub_topic
        if sub is not None:
            result.sub_topic_ref = sub
            result.sub_topic = f"{sub.code} {sub.name}"
        else:
            result.sub_topic = subtype
        logger.info("SUBTYPE-CORRECTION raw_sub=%r -> %s (deterministic keyword override)",
                    old, result.sub_topic)

    logger.info("CLASSIFIER-CATEGORY category=%r sub_topic=%r", result.category, result.sub_topic)
    return result


def classify(brand, message, provider=None):
    """Classify a normalized message dict (low-level helper).

    Uses the AI provider with retry; falls back to the keyword rule-classifier only
    after retries fail (or when no provider is configured). Returns None only when
    there's no provider AND the rule fallback is disabled.
    """
    provider = provider or build_provider(_settings_for(brand))
    if provider is None:
        if _rule_fallback_enabled():
            logger.info("No AI provider for %s; using rule-based classifier.", brand)
            return _post_correct(brand, message, _rule_classify(brand, message))
        return None

    try:
        return _post_correct(brand, message, ai_generate(brand, message, provider))
    except Exception as exc:  # noqa: BLE001 -- AI down / quota -> fall back to rules
        if _rule_fallback_enabled():
            logger.warning("AI classify failed (%s); using rule-based fallback.", exc)
            return _post_correct(brand, message, _rule_classify(brand, message))
        raise


def _message_dict_from_ticket(ticket):
    """Build the classifier input from a ticket's most recent inbound mail."""
    msg = (
        ticket.messages.filter(direction=Message.DIRECTION_INBOUND)
        .order_by("-created_at")
        .first()
    )
    if msg is None:
        return {
            "from_email": ticket.customer_email,
            "subject": ticket.subject,
            "body_text": "",
            "attachments": [],
        }
    return {
        "from_email": msg.from_email,
        "subject": msg.subject or ticket.subject,
        "body_text": msg.body_text,
        "snippet": (msg.body_text or msg.body_html or "")[:500],
        "attachments": msg.attachments or [],
    }


def apply_to_ticket(ticket, result, classification_status=None, ai_error=""):
    """Persist a ClassificationResult onto a ticket and audit it (doc sections 4, 11).

    classification_status: Ticket.CLS_CLASSIFIED (Gemini) or CLS_FAILED (rule fallback).
    """
    ticket.category = result.category
    ticket.sub_topic = result.sub_topic
    ticket.category_ref = result.category_ref
    ticket.sub_topic_ref = result.sub_topic_ref
    ticket.ai_confidence = result.confidence
    ticket.language = result.language
    ticket.sentiment = result.sentiment
    ticket.issue_summary = result.issue_summary or ticket.issue_summary
    if classification_status:
        ticket.classification_status = classification_status
    ticket.ai_error = ai_error
    merged = {**ticket.extracted, **(result.extracted or {})}
    # Spec fields the engine consults: requires_evidence / requires_agent / etc.
    merged.update({
        "issue_summary": result.issue_summary,
        "requires_evidence": result.requires_evidence,
        "requires_agent": result.requires_agent,
        "ai_action": result.action,
    })
    ticket.extracted = merged
    if result.sub_topic_ref is not None:
        ticket.mandatory_inputs = result.sub_topic_ref.mandatory_inputs

    # Not a support request -> move to the Ignored tab (no ticket work, no reply). BUT if
    # the AI assigned a CONCRETE taxonomy category (1..16, not "Uncategorized"), the email
    # IS an actionable customer inquiry -- trust the category and do NOT ignore it. This
    # stops genuine franchise / seller / B2B / wholesale inquiries (category 11) from being
    # dropped as "promotional". True non-support mail (newsletters / OTP / no-reply) lands
    # as Uncategorized, or is caught earlier by the sender ignore-gate.
    from .skills import UNCATEGORIZED

    category = (result.category or "").strip()
    if not result.is_support_request and category in ("", UNCATEGORIZED):
        ticket.is_ignored = True
        ticket.ignored_reason = "AI: not a support request"
        ticket.status = Ticket.STATUS_IGNORED
        ticket.save()
        AuditLogEntry.objects.create(
            ticket=ticket, actor="ai", event="ignored",
            detail={"reason": "not_support_request", "category": result.category},
        )
        return ticket
    if not result.is_support_request:
        logger.info("AI flagged not-support but assigned category %r -> treating as a "
                    "support request (ticket=%s).", category, ticket.ticket_id)

    if ticket.status == Ticket.STATUS_NEW:
        ticket.status = Ticket.STATUS_CLASSIFIED
    ticket.save()

    AuditLogEntry.objects.create(
        ticket=ticket,
        actor="ai",
        event="classified",
        detail={
            "category": result.category,
            "sub_topic": result.sub_topic,
            "confidence": result.confidence,
            "uncategorized": result.is_uncategorized,
            "sensitive": bool(
                result.sub_topic_ref and result.sub_topic_ref.is_sensitive
            ),
        },
    )
    return ticket


def classify_ticket(ticket, provider=None):
    """Classify a ticket through the full lifecycle (spec):

      PENDING_AI -> AI_PROCESSING -> AI_CLASSIFIED   (Gemini succeeded)
                                  -> AI_FAILED        (retries failed -> rule fallback)

    Tries the AI provider with retry/backoff. Only after retries fail does it use the
    rule-based fallback (status AI_FAILED). AI errors are stored on the ticket and
    audited (visible in Django admin). Returns the ClassificationResult, or None.
    """
    if ticket.is_ignored:
        return None

    brand = ticket.brand
    message = _message_dict_from_ticket(ticket)
    provider = provider or build_provider(_settings_for(brand))

    ticket.classification_status = Ticket.CLS_PROCESSING
    ticket.save(update_fields=["classification_status", "updated_at"])

    # 1) Try Gemini with retry/backoff.
    if provider is not None:
        try:
            result = ai_generate(brand, message, provider)
            apply_to_ticket(ticket, result, classification_status=Ticket.CLS_CLASSIFIED)
            return result
        except Exception as exc:  # noqa: BLE001 -- retries exhausted
            ticket.ai_attempts = (ticket.ai_attempts or 0) + 1
            ticket.ai_error = str(exc)[:1000]
            ticket.classification_status = Ticket.CLS_FAILED
            ticket.save(update_fields=[
                "ai_attempts", "ai_error", "classification_status", "updated_at"])
            AuditLogEntry.objects.create(
                ticket=ticket, actor="ai", event="ai_error",
                detail={"error": ticket.ai_error, "attempts": ticket.ai_attempts},
            )
            logger.error("AI classification FAILED for %s: %s", ticket.ticket_id, exc)

    # 2) Fallback to rules ONLY after AI failed / no provider (spec rule 4).
    if not _rule_fallback_enabled():
        if provider is None:
            ticket.classification_status = Ticket.CLS_FAILED
            ticket.save(update_fields=["classification_status", "updated_at"])
        return None
    result = _rule_classify(brand, message)
    apply_to_ticket(ticket, result, classification_status=Ticket.CLS_FAILED,
                    ai_error=ticket.ai_error)
    return result
