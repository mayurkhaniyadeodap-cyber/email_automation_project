"""
Per-category business rules (the customer's spec). These act as a guardrail on top
of the sub-topic IF/THEN engine: money / account / fraud categories must NEVER be
auto-replied, no matter what a sub-topic rule says, while safe info categories may.

Category codes are the fixed 1..16 taxonomy:
    AUTO_REPLY  : auto-answer allowed   -> 1 Shipment, 9 Product Info, 10 Offers, 12 Coverage
    DRAFT_AGENT : draft reply + agent   -> 6 Order Cancellation, 7 Return/Refund/Replacement
    AGENT       : assign agent (no auto) -> 8 Payment & Invoice, 14 Account & Security
    ESCALATE    : agent immediately, high -> 16 Feedback, Support & Fraud
Anything not listed falls through to the sub-topic decision engine unchanged.
"""

AUTO_REPLY = {"1", "9", "10", "12"}
DRAFT_AGENT = {"6", "7"}
AGENT = {"8", "14"}
ESCALATE = {"16"}

POLICY_AUTO_REPLY = "auto_reply"
POLICY_DRAFT_AGENT = "draft_agent"
POLICY_AGENT = "agent"
POLICY_ESCALATE = "escalate"


def policy_for(category_code):
    """Return the business-rule policy for a category code, or None (no constraint)."""
    code = str(category_code or "").strip()
    if code in AUTO_REPLY:
        return POLICY_AUTO_REPLY
    if code in DRAFT_AGENT:
        return POLICY_DRAFT_AGENT
    if code in AGENT:
        return POLICY_AGENT
    if code in ESCALATE:
        return POLICY_ESCALATE
    return None


def allows_auto_reply(category_code):
    """True if this category may be auto-answered (no policy, or explicitly auto)."""
    return policy_for(category_code) in (None, POLICY_AUTO_REPLY)


# --------------------------------------------------------------------------- #
# Auto-Reply vs Ticket routing (the customer's authoritative taxonomy). SINGLE source of
# truth for whether a message becomes a Care Panel ticket (+ tracking link) or is fully
# answered by an auto-reply (no ticket, no link). Resolution order:
#   1. TICKET sub-topic keywords   (logistics/item/account-change/website-fault/payment/fraud)
#   2. AUTO-REPLY sub-topic keywords (tracking/item-or-GST edits/offers/delete-account/inquiry)
#   3. Category-code fallback.
# Keyword routing is checked first so ONE category can split -- e.g. cat 1: "Shipment
# Tracking" auto-replies but "Delayed/Undelivered/Reschedule" create tickets; cat 14:
# "Delete Account / Data Privacy" auto-reply but "Update Phone/Email / OTP" create tickets.
# --------------------------------------------------------------------------- #

ROUTE_TICKET = "create_ticket"
ROUTE_AUTO_REPLY = "auto_reply"

# Categories that create a ticket by default (when no keyword above matched).
TICKET_CATEGORIES = {
    "2",   # Delivery Address & Customer Info Changes (update address / phone)
    "3",   # Delivery Issues (damaged / defective / missing / wrong / quantity / quality)
    "4",   # RTO (Return to Origin) -> Cancelled Delivery
    "6",   # Order Cancellation
    "7",   # Return, Refund & Replacement (refund status)
    "8",   # Payment & Invoice -> Payment Issue (invoice COPY is split out below)
    "15",  # App & Website faults (crashing / cart not saving / checkout not loading)
    "16",  # Feedback, Support & Fraud -> fraud investigation (HIGH)
}

# Categories that auto-reply by default (information / self-serve -> no ticket).
NO_TICKET_CATEGORIES = {
    "1",   # Shipment & Delivery Tracking (logistics ACTIONS split to ticket via keywords)
    "5",   # Order Placement & Modification (add / update items, GST details)
    "9",   # Product Information & Inquiry
    "10",  # Offers, Discounts & Loyalty -> verify -> auto-reply (no ticket)
    "11",  # Wholesale / Bulk Purchase -> Inquiry (Bulk Order Inquiry / VIP Bulk Pricing)
    "12",  # Delivery Coverage & Shipping
    "13",  # Store & Company Information
    "14",  # Account help (delete account / data privacy / password) -- changes split below
}

# TICKET sub-topics (human action / investigation). Checked FIRST -- these WIN over the
# auto-reply set and the category fallback.
_TICKET_KW = (
    # Delivery logistics actions (cat 1/2/4)
    "delayed delivery", "delivery delayed", "delivery is delayed", "undelivered",
    "out for delivery", "urgent request", "urgent delivery", "delivery time info",
    "call delivery agent", "call the delivery agent", "call agent", "reschedule delivery",
    "reschedule", "refund status", "check refund", "delivery time",
    # Delivered-item issues (cat 3)
    "damaged item", "defective item", "quality issue", "missing item", "wrong item",
    "quantity issue", "other issue", "delivered but not received", "shown delivered",
    # Order info change (cat 2)
    "update address", "change address", "update phone", "change phone", "update my phone",
    # Website / app technical FAULTS (cat 15)
    "app crash", "not loading", "crashing", "cart not saving", "not saving", "checkout page",
    "checkout not", "checkout page not load",
    # Account CONTACT change / OTP (cat 14 -> ticket)
    "update email", "change email", "update my email", "otp not", "no otp", "receive otp",
    "not receiving otp", "did not receive otp", "didn't receive otp", "not getting otp",
    "notifications not received", "notification not received", "not receiving notification",
    # Payment problem / fraud
    "payment issue", "payment problem", "charged twice", "double charge", "double charged",
    "charged", "overcharged", "deducted", "money debited", "amount debited", "payment failed",
    "failed payment", "not refunded", "refund not", "fraud", "scam", "fraudster",
    "suspicious call", "fake payment",
)

# AUTO-REPLY sub-topics (information / self-serve). Checked AFTER the ticket set.
_AUTO_REPLY_KW = (
    # Help with order -> tracking
    "shipment tracking", "track order", "track my order", "where is my order", "tracking id",
    "tracking number", "track shipment",
    # Make changes to order -> item / GST edits (self-serve)
    "add item", "update item", "add items", "update items", "add / update item",
    "add gst", "update gst", "gst detail", "gst number", "add / update gst",
    # Offers & Sales -> auto-reply (after verification; no ticket)
    "offer", "discount", "sale", "coupon", "loyalty", "deal",
    # Account self-serve (no ticket)
    "delete account", "delete my account", "close account", "deactivate account",
    "data privacy", "data & privacy", "privacy security", "data security", "privacy policy",
    "reset password", "forgot password", "password", "order history", "saved address",
    # Invoice COPY (information; the full invoice flow is the Inquiry workflow)
    "invoice copy", "copy of invoice", "send invoice", "share invoice", "download invoice",
    "gst invoice", "tax invoice", "bill copy", "invoice please", "need invoice",
    # Inquiry / bulk (also intercepted earlier by the Inquiry workflow)
    "franchise", "dropship", "company profile", "other inquiry", "bulk order inquiry",
    "vip bulk", "vip pricing", "wholesale inquiry", "bulk inquiry",
)


# HARD BLOCK: intents that must NEVER create a ticket -- auto-reply ONLY -- regardless of any
# keyword / category rule or how the classifier labelled them. "Make Changes To Order -> Add /
# Update Items | Add / Update GST Details" are answered with a fixed reply and the conversation
# is closed (no ticket ever). Matched by NATURAL phrasing (not just the exact taxonomy label),
# so "Add one more item to my order" and "Add and update GST details" are both caught.
_NO_TICKET_ITEM_KW = ("add / update items", "add/update items", "update items",
                      "add one more item", "add more item", "add another item", "add an item",
                      "add a item", "add item", "add items", "add a product", "add more product",
                      "add product to", "extra item", "additional item", "include another item",
                      "add to my order", "add to order", "one more item", "another item to")
_NO_TICKET_GST_KW = ("add / update gst", "add/update gst", "add and update gst",
                     "add or update gst", "gst details", "gst detail", "gst number", "gstin",
                     "add gst", "update gst", "change gst", "edit gst", "modify gst")


def no_ticket_flow(text):
    """Return 'gst_update' / 'add_items' when `text` expresses an Add-Update-GST / Add-Update-
    Items intent, else None. These are ALWAYS auto-reply (never a ticket / existing-ticket
    lookup), whatever the classifier decided."""
    low = (text or "").lower()
    if any(k in low for k in _NO_TICKET_GST_KW):
        return "gst_update"
    if any(k in low for k in _NO_TICKET_ITEM_KW):
        return "add_items"
    return None


def blocks_ticket(category="", sub_topic="", text=""):
    """Mandatory safety check: True if this message must NEVER become a ticket (Add/Update Items
    / Add/Update GST under Make Changes To Order). Callers MUST short-circuit to an auto-reply
    when this returns True -- never call create_ticket() / find_existing_ticket()."""
    return no_ticket_flow(f"{sub_topic or ''} {text or ''}") is not None


# Offers / discounts / coupons -> "Ongoing Offers & Sales" (cat 10), ALWAYS auto-reply -- NEVER
# a Delivery / Tracking / Missing-Item issue. Word-boundary matched so 'wholesale'/'dealer' do
# NOT trigger ('sale'/'deal').
import re as _re  # noqa: E402

_OFFERS_RE = _re.compile(
    r"\b(offers?|discounts?|sales?|coupons?|promo(?:tions?)?|promo\s*codes?|deals?|cashback|"
    r"loyalty)\b", _re.IGNORECASE)
_OFFERS_PROBLEM_KW = ("not work", "not applying", "not applied", "not apply", "not visible",
                      "not showing", "not show", "invalid", "expired", "error", "issue",
                      "problem", "failed", "cant apply", "can't apply", "cannot apply",
                      "won't apply", "not getting", "didn't get", "wrong price", "isn't applying")


def offers_flow(text):
    """Return 'offers_problem' (a discount/coupon NOT working) or 'offers_general' (a general
    offers inquiry) when `text` is about offers / discounts / coupons / sales / promos / deals,
    else None. Offers are ALWAYS auto-reply (Ongoing Offers & Sales), never a delivery/item."""
    if not _OFFERS_RE.search(text or ""):
        return None
    low = (text or "").lower()
    return "offers_problem" if any(k in low for k in _OFFERS_PROBLEM_KW) else "offers_general"


# Payment deducted / debited but the order was NOT placed -> ALWAYS a Payment Issue (cat 8),
# sub-topic "Payment Deducted But Order Not Placed". NEVER a Delivery / Tracking / Item issue.
_PAYMENT_NO_ORDER_RE = _re.compile(
    r"\b(payment|paid|payed|pay|amount|money|transaction|deducted|debited|charged|upi|gpay|"
    r"phonepe|paytm)\b.{0,60}?\b("
    # order <something> not placed/created/received... (typo-tolerant: palced/recieved)
    r"order\b.{0,14}?\bnot\s+(?:placed|palced|place|created|received|recieved|confirmed|"
    r"generated|done)"
    r"|no\s+order|without\s+(?:an?\s+)?order|order\s+missing)",
    _re.IGNORECASE | _re.DOTALL)
_PAYMENT_NO_ORDER_KW = (
    "payment deducted but order not placed", "money deducted but order not received",
    "amount debited but no order", "payment successful but order not created",
    "payment completed but order missing", "order not placed after payment",
    "transaction successful but no order", "made a payment but the order was not placed",
    "made payment but order not placed", "paid but order not placed", "payment issue")


def payment_no_order(text):
    """True when the message is 'payment deducted/debited but the order was not placed' (in any
    of its phrasings) -- ALWAYS a Payment Issue ticket, never a delivery/item category."""
    low = (text or "").lower()
    if any(k in low for k in _PAYMENT_NO_ORDER_KW):
        return True
    return bool(_PAYMENT_NO_ORDER_RE.search(text or ""))


# HIGH-PRIORITY ESCALATION: legal / consumer-court / grievance / police / media / reputation.
# Detecting ANY of these STOPS all automation -> the email goes to the manual-review queue, no
# ticket and no automatic customer email. Word-boundary matched (longest phrase first) so e.g.
# 'COURT' never fires on 'courtesy', 'NCH' never inside 'launch', 'SUE' never inside 'issue'.
_ESCALATION_KEYWORDS = (
    # Consumer / Legal
    "NATIONAL CONSUMER HELPLINE", "DISTRICT CONSUMER COURT", "STATE CONSUMER COURT",
    "NATIONAL CONSUMER COURT", "CONSUMER COMMISSION", "CONSUMER COURT", "CONSUMER FORUM", "NCH",
    # Government / Complaint
    "GRAHAK SEVA KENDRA", "GRAHAK SEVA", "GRAHAK", "PUBLIC GRIEVANCE", "GRIEVANCE", "GREIVANCE",
    "COMPLAINT CELL",
    # Police / Cyber
    "CYBER CRIME", "CYBERCRIME", "CYBER CELL", "POLICE COMPLAINT", "CRIME BRANCH", "POLICE", "FIR",
    # Legal
    "LEGAL NOTICE", "LEGAL ACTION", "LITIGATION", "ADVOCATE", "ABHIVAKTA", "LAWYER", "VAKIL",
    "NOTICE", "COURT", "CASE", "SUING", "SUE",
    # Management
    "HEAD OFFICE", "MANAGEMENT", "DIRECTOR", "FOUNDER", "OWNER", "CEO",
    # Reputation
    "NEGATIVE REVIEW", "NEGETIVE REVIEW", "GOOGLE REVIEW", "FACEBOOK REVIEW", "BAD REVIEW",
    "SOCIAL MEDIA", "TRUSTPILOT", "INSTAGRAM", "LINKEDIN", "YOUTUBE", "TWITTER", "X.COM",
    # Media
    "NEWSPAPER", "REPORTER", "CHANNEL", "PRESS", "MEDIA", "NEWS")


def _kw_pattern(kw):
    # Whitespace-flexible (matches 'consumer\ncourt' / 'consumer  court' too).
    return r"\s+".join(_re.escape(tok) for tok in kw.split())


_ESCALATION_RE = _re.compile(
    r"\b(" + "|".join(_kw_pattern(k) for k in
                      sorted(_ESCALATION_KEYWORDS, key=len, reverse=True)) + r")\b",
    _re.IGNORECASE)


def escalation_keyword(text):
    """Return the MATCHED escalation keyword (UPPER-cased, whitespace-normalised) if `text`
    contains any legal/grievance/police/media/reputation keyword, else None. Case-insensitive,
    word-boundary aware, longest phrase first. Detection => STOP all automation."""
    m = _ESCALATION_RE.search(text or "")
    return " ".join(m.group(1).upper().split()) if m else None


def route_category(category_code, sub_topic="", text=""):
    """Authoritative routing: CREATE_TICKET or AUTO_REPLY for a classified message, per the
    customer's taxonomy. Keyword routing (ticket-first, then auto) wins over the category
    fallback so a single category can split. Unknown / uncategorized -> CREATE_TICKET."""
    code = str(category_code or "").split(".")[0].strip()
    blob = " ".join([sub_topic or "", text or ""]).lower()

    if blocks_ticket(sub_topic=sub_topic, text=text):  # HARD BLOCK -> auto-reply, no ticket ever
        return ROUTE_AUTO_REPLY
    if any(k in blob for k in _TICKET_KW):
        return ROUTE_TICKET
    if any(k in blob for k in _AUTO_REPLY_KW):
        return ROUTE_AUTO_REPLY
    if code in TICKET_CATEGORIES:
        return ROUTE_TICKET
    if code in NO_TICKET_CATEGORIES:
        return ROUTE_AUTO_REPLY
    return ROUTE_TICKET                               # unknown -> safe side (a human reviews)


def requires_ticket(category_code, sub_topic="", text=""):
    """True if this message must become a Care Panel ticket (the inverse of an auto-reply).
    Thin wrapper over route_category() -- the authoritative ticket-vs-auto-reply table."""
    return route_category(category_code, sub_topic, text) == ROUTE_TICKET
