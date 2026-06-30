"""
Keyword rule-based classifier -- a deterministic fallback used when no AI provider
is configured or the AI call fails (e.g. Gemini rate-limit / quota). It produces the
SAME JSON shape Gemini returns, so the rest of the pipeline is unchanged.

Not as nuanced as the LLM, but it keeps the engine running end-to-end with zero cost
and zero quota. The taxonomy is still the source of truth for category/sub-topic refs.
"""

import logging
import re

from .skills import UNCATEGORIZED

logger = logging.getLogger(__name__)

# Category code -> trigger keywords (the fixed 16-category taxonomy).
KEYWORDS = {
    "1": ["track", "tracking", "where is my order", "shipment", "delivery status",
          "when will", "arrive", "courier", "dispatched", "out for delivery"],
    "2": ["change address", "wrong address", "update address", "change my address",
          "correct address", "edit address", "change number"],
    "3": ["damaged", "damage", "broken", "not received", "missing item", "defective",
          "wrong item", "leaked", "empty box", "tampered", "spoiled", "received damaged",
          "received damage", "damage order", "damage item", "damage product"],
    "4": ["return to origin", "rto", "returned to seller", "undelivered", "sent back"],
    "5": ["place order", "modify order", "change order", "add item", "order not confirmed",
          "edit order", "wrong order placed"],
    "6": ["cancel", "cancellation", "cancel my order", "cancel order"],
    "7": ["refund", "return", "replace", "replacement", "money back", "exchange",
          "want my money", "return the product", "want to change", "want to replace",
          "change it", "change my order", "change the order"],
    "8": ["payment", "invoice", "charged", "double charge", "gst", "bill", "receipt",
          "paid twice", "extra charge", "transaction"],
    "9": ["product detail", "specification", "size", "color", "colour", "material",
          "how to use", "available", "in stock", "features"],
    "10": ["coupon", "offer", "discount", "promo", "loyalty", "cashback", "deal", "sale"],
    "11": ["bulk", "wholesale", "b2b", "reseller", "dealer", "distributor", "bulk order"],
    "12": ["deliver to", "serviceable", "pincode", "pin code", "cod available", "do you deliver",
           "delivery available", "shipping to"],
    "13": ["store", "your address", "contact number", "about your", "gst number",
           "company", "office location"],
    "14": ["account", "password", "login", "otp", "delete account", "security", "sign in",
           "reset password", "block my account"],
    "15": ["app", "website", "error", "not working", "crash", "bug", "page not loading",
           "glitch", "can't login"],
    "16": ["fraud", "scam", "complaint", "feedback", "fake", "cheated", "report",
           "harassment", "abuse"],
}

# Categories that always need a human (mirrors decision policy).
_AGENT_CODES = {"6", "7", "8", "14", "16"}

# Signals that an email is NOT a customer support request (-> Ignored, no ticket).
# Conservative on purpose: specific phrases, so "report fraud" stays a support ticket.
_NON_SUPPORT_SUBJECT = [
    "weekly report", "daily report", "final report", "pending report",
    "monthly report", "summary report", "status report", "dispatch pending",
    "courier final", "newsletter", "unsubscribe", "do not reply", "auto-generated",
    "delivery report", "remittance report",
]
_NON_SUPPORT_SENDER = [
    "noreply", "no-reply", "no_reply", "donotreply", "notifications@",
    "notification@", "mailer-daemon", "postmaster", "alerts@", "updates@",
]


def _is_support_request(low, from_email):
    fe = (from_email or "").lower()
    if any(s in fe for s in _NON_SUPPORT_SENDER):
        return False
    if any(phrase in low for phrase in _NON_SUPPORT_SUBJECT):
        return False
    return True
_ANGRY = ["angry", "worst", "pathetic", "cheated", "fraud", "scam", "terrible", "useless"]
_FRUSTRATED = ["disappointed", "frustrated", "still waiting", "no response", "again and again"]
_EVIDENCE_PRESENT = ["photo", "image", "video", "attached", "attachment", "picture"]
# Bare order reference: a DD-prefixed code or a long (6+ digit) number. The 6-digit
# floor avoids grabbing pincodes/short numbers that appear without context.
_ORDER_RE = re.compile(r"\b(DD\d{3,}|#?\d{6,})\b", re.IGNORECASE)
# Context-aware: when the customer SAYS it's their order ("order number is 12345",
# "order id: DD9999", "order #4564530"), accept a shorter (3+ digit) reference too --
# the "order" keyword makes a 5-digit number unambiguous.
_ORDER_CONTEXT_RE = re.compile(
    r"order\s*(?:number|no\.?|num|id)?\s*(?:is|are|[:=#\-])*\s*([A-Za-z]{0,4}\d{3,})\b",
    re.IGNORECASE,
)
# Indian mobile: optional +91/0, then a 10-digit number starting 6-9.
_PHONE_RE = re.compile(r"(?:\+?91[-\s]?|0)?([6-9]\d{9})\b")


def _extract_order_id(text):
    text = text or ""
    # Prefer an explicitly-stated order reference (handles short numbers like 12345).
    m = _ORDER_CONTEXT_RE.search(text)
    if m:
        return m.group(1).lstrip("#")
    m = _ORDER_RE.search(text)
    if not m:
        return None
    value = m.group(1).lstrip("#")
    # Bare fallback: a PHONE-shaped number (10-digit mobile starting 6-9, or the same with a
    # +91 / 91 / 0 prefix) is a phone, NEVER an order. The explicit "order <ref>" case was
    # already handled by the context regex above, so here we always reject phone numbers --
    # even when the word "order" appears elsewhere ("cancel my order. my mobile is 99074...").
    if normalize_phone(value):
        return None
    # A bare number presented in a PHONE/MOBILE context (and NOT an order context, which
    # the regex above already handled) is the customer's phone number, not an order ref --
    # e.g. "PHONE NUMBER : 45678912352". Don't capture it as an order id.
    low = text.lower()
    if any(k in low for k in ("phone", "mobile", "contact number", "whatsapp", "call me")):
        return None
    return value


def normalize_phone(raw):
    """Canonical Indian mobile = the bare 10 digits (strip +91 / 0 / spaces / dashes).
    Returns '' when the input is not a 10-digit 6-9 mobile. This is the SINGLE form used
    everywhere so extraction and the Shopify lookup never disagree on format."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else ""


def _extract_phone(text):
    m = _PHONE_RE.search(text or "")
    if not m:
        logger.info("PHONE-REGEX-MATCH none raw=%r", (text or "")[:200])
        return None
    raw_match = m.group(1)
    normalized = normalize_phone(raw_match) or raw_match
    logger.info("PHONE-REGEX-MATCH %s | PHONE-PARSER-MATCH %s | PHONE-NORMALIZED %s",
                raw_match, raw_match, normalized)
    return normalized


def _sentiment(low):
    if any(w in low for w in _ANGRY):
        return "angry"
    if any(w in low for w in _FRUSTRATED):
        return "frustrated"
    return "neutral"


def _best_subtopic(sub_topics, low):
    """Pick the sub-topic whose name shares the most words with the email."""
    best, best_score = None, 0
    for sub in sub_topics:
        words = [w for w in re.findall(r"[a-z]+", sub.name.lower()) if len(w) > 3]
        score = sum(1 for w in words if w in low)
        if score > best_score:
            best, best_score = sub, score
    return best


def build_data(brand, message):
    """Return a Gemini-shaped classification dict from keyword rules."""
    from apps.taxonomy.models import Category

    subject = message.get("subject") or ""
    body = message.get("body_text") or message.get("snippet") or ""
    raw = f"{subject} {body}"
    low = raw.lower()

    scores = {code: sum(1 for kw in kws if kw in low) for code, kws in KEYWORDS.items()}
    best_code, best_score = max(scores.items(), key=lambda kv: kv[1])
    order_id = _extract_order_id(raw)
    phone = _extract_phone(raw)
    sentiment = _sentiment(low)
    is_support = _is_support_request(low, message.get("from_email", ""))

    if not is_support or best_score == 0:
        return {
            "is_support_request": is_support,
            "category": UNCATEGORIZED, "sub_topic": "", "confidence": 0.3,
            "issue_summary": subject[:120], "requires_evidence": False,
            "requires_agent": is_support,  # uncategorized-but-support -> agent
            "action": "ignore" if not is_support else "assign_agent",
            "extracted": {"order_id": order_id, "phone": phone}, "sentiment": sentiment, "language": "en",
        }

    cat = Category.objects.filter(brand=brand, code=best_code).first()
    sub = None
    if cat:
        subs = list(cat.sub_topics.filter(is_active=True).order_by("position", "code"))
        sub = _best_subtopic(subs, low) or (subs[0] if subs else None)

    requires_evidence = best_code == "3" and not any(w in low for w in _EVIDENCE_PRESENT)
    requires_agent = best_code in _AGENT_CODES
    if requires_evidence:
        action = "request_evidence"
    elif requires_agent:
        action = "assign_agent"
    else:
        action = "auto_reply"

    return {
        "is_support_request": True,
        "category": f"{best_code}. {cat.name if cat else ''}",
        "sub_topic": f"{sub.code} {sub.name}" if sub else "",
        "confidence": 0.6,  # rule-based: moderate confidence
        "issue_summary": subject[:120] or body[:120],
        "requires_evidence": requires_evidence,
        "requires_agent": requires_agent,
        "action": action,
        "extracted": {"order_id": order_id, "phone": phone},
        "sentiment": sentiment,
        "language": "en",
    }
