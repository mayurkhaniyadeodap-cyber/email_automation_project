"""
Inquiry workflow -- a DEDICATED multi-step conversational flow for business inquiries,
completely separate from the support / complaint / order-verification flow.

5 inquiry types: FRANCHISEE, DROPSHIPPING, COMPANY_PROFILE, INVOICE_REQUEST, OTHER_INQUIRY.

This module is PURE logic (no DB, no email): keyword detection, the per-type flow spec
(confirmation gate + ordered field-collection steps + final message), and the state-machine
helpers. The orchestration (pending state, sending mail, creating Inquiry records) lives in
apps.ingestion.service, which calls into here.

RULES (enforced by the caller routing into these flows FIRST): an inquiry NEVER asks for
order verification / registered email / AWB, never enters the support-ticket or complaint
workflow, and never triggers the M1 verification template.
"""

import re

# --- intent + sub-types -----------------------------------------------------------------
INTENT = "INQUIRY"
FRANCHISEE = "FRANCHISEE"
DROPSHIPPING = "DROPSHIPPING"
COMPANY_PROFILE = "COMPANY_PROFILE"
INVOICE_REQUEST = "INVOICE_REQUEST"
OTHER_INQUIRY = "OTHER_INQUIRY"
# Sub-flows reached via a MENU (the customer picks one after the category is detected).
BULK_ORDER = "BULK_ORDER"
VIP_BULK_PRICING = "VIP_BULK_PRICING"
FRAUD_PAYMENT = "FRAUD_PAYMENT"
FRAUD_ALERT = "FRAUD_ALERT"
# Menu CATEGORIES -- detected from keywords, then show a menu of sub-flows.
BULK_PURCHASE = "BULK_PURCHASE"
REPORT_FRAUD = "REPORT_FRAUD"

INQUIRY_TYPES = (FRANCHISEE, DROPSHIPPING, COMPANY_PROFILE, INVOICE_REQUEST, OTHER_INQUIRY,
                 BULK_ORDER, VIP_BULK_PRICING, FRAUD_PAYMENT, FRAUD_ALERT)
MENU_CATEGORIES = (BULK_PURCHASE, REPORT_FRAUD)

# Keyword -> type. ORDER MATTERS: most-specific first so "gst invoice" -> INVOICE_REQUEST and
# "business partner" -> DROPSHIPPING (not OTHER). OTHER_INQUIRY only matches explicit generic
# inquiry phrases -- it is NOT a catch-all (that would hijack ordinary support emails).
# ORDER MATTERS. Fraud + Bulk are checked FIRST (a fraud report must never fall into invoice/
# support, and "reseller bulk" must be BULK not DROPSHIPPING).
INQUIRY_KEYWORDS = [
    (REPORT_FRAUD, ("fraud", "scam", "fake payment", "suspicious call", "fake whatsapp",
                    "fraudster", "cheated", "payment done to fraud", "suspicious number",
                    "fraud call", "scam call", "fraud payment", "paid a fraud", "paid to fraud",
                    "asking otp", "asking for otp", "called asking", "fake call", "spam call",
                    "suspicious mobile", "fraud person")),
    (BULK_PURCHASE, ("bulk purchase", "wholesale", "bulk order", "bulk inquiry",
                     "wholesale inquiry", "reseller bulk", "large quantity order",
                     "corporate order")),
    (INVOICE_REQUEST, ("invoice", "gst invoice", "tax invoice", "bill copy", "copy of bill",
                       "need invoice", "send invoice", "want invoice")),
    (COMPANY_PROFILE, ("company profile", "brochure", "catalog", "catalogue",
                       "business details", "company information", "company details")),
    (DROPSHIPPING, ("dropshipping", "drop shipping", "drop-ship", "dropship", "reseller",
                    "reseller program", "seller program", "business partner")),
    (FRANCHISEE, ("franchisee", "franchise", "dealership inquiry", "dealership",
                  "own franchise")),
    (OTHER_INQUIRY, ("business inquiry", "business enquiry", "partnership inquiry",
                     "partnership", "general inquiry", "general enquiry", "other inquiry")),
]

# Menu shown for a menu CATEGORY, and how a reply maps to a sub-flow.
MENUS = {
    BULK_PURCHASE: {
        "prompt": "Thank you for your interest in bulk purchasing. Please choose an option:\n\n"
                  "1. Bulk Order Inquiry\n2. VIP Bulk Pricing\n\nReply with 1 or 2.",
        "options": [("1", ("1", "bulk order", "bulk order inquiry"), BULK_ORDER),
                    ("2", ("2", "vip", "vip bulk", "vip pricing", "vip bulk pricing"),
                     VIP_BULK_PRICING)],
    },
    REPORT_FRAUD: {
        "prompt": "We are here to help. Please choose an option:\n\n"
                  "1. Payment Done to Fraudster\n2. Get Suspicious Call\n\nReply with 1 or 2.",
        "options": [("1", ("1", "payment", "payment done", "fraudster", "paid"), FRAUD_PAYMENT),
                    ("2", ("2", "suspicious call", "suspicious", "call", "get suspicious call"),
                     FRAUD_ALERT)],
    },
}


def is_menu_category(value):
    return value in MENU_CATEGORIES


def menu_prompt(category):
    return MENUS[category]["prompt"]


def resolve_menu_choice(category, text):
    """Map a menu reply to a sub-flow type, or None if it doesn't match an option."""
    low = (text or "").strip().lower()
    for _num, keywords, subtype in MENUS[category]["options"]:
        if any(k in low for k in keywords):
            return subtype
    return None


# First-email free text -> the SPECIFIC sub-flow, so we skip the option menu when the
# sub-category is already clear ("I paid a fraud person" -> FRAUD_PAYMENT directly). Ordered:
# most-decisive first. Returns None for a generic issue ("I have a fraud issue") -> show menu.
_MENU_SUBTYPE_KW = {
    REPORT_FRAUD: [
        (FRAUD_PAYMENT, ("paid", "fraud payment", "payment done", "payment to fraud",
                         "paid a fraud", "paid to", "sent money", "transferred", "made a payment",
                         "money to fraud", "done to fraudster", "lost money", "deducted money")),
        (FRAUD_ALERT, ("suspicious call", "get suspicious call", "fraud call", "scam call",
                       "fake call", "spam call", "call received", "received a call",
                       "someone called", "called asking", "asking otp", "asking for otp",
                       "got a call", "suspicious number", "suspicious mobile", "phone call")),
    ],
    BULK_PURCHASE: [
        (VIP_BULK_PRICING, ("vip", "vip pricing", "vip bulk")),
        (BULK_ORDER, ("bulk order", "bulk inquiry", "bulk quantity")),
    ],
}


def detect_menu_subcategory(category, text):
    """From a FIRST email's free text, return the specific sub-flow when it is already clear
    (so we SKIP the option menu), else None (-> show the menu so the customer picks)."""
    low = (text or "").lower()
    for subtype, kws in _MENU_SUBTYPE_KW.get(category, []):
        if any(k in low for k in kws):
            return subtype
    return None

_AFFIRMATIVE = ("yes", "yeah", "yep", "yup", "sure", "ok", "okay", "interested",
                "proceed", "confirm", "definitely", "absolutely")


def detect_inquiry_type(text):
    """Return one of INQUIRY_TYPES if `text` (subject + body) is a business inquiry, else
    None. Deterministic and order-sensitive."""
    low = (text or "").lower()
    for itype, keywords in INQUIRY_KEYWORDS:
        if any(k in low for k in keywords):
            return itype
    return None


def is_affirmative(text):
    """True when the reply is a YES to the confirmation gate."""
    low = (text or "").strip().lower()
    if not low:
        return False
    if "not interested" in low or low.startswith("no"):
        return False
    words = set(re.findall(r"[a-z]+", low))
    return bool(words & set(_AFFIRMATIVE))


# --- SINGLE-REPLY flow spec --------------------------------------------------------------
# Every field-collecting flow asks for ALL required details in ONE message and parses the
# customer's single reply ("Name: X  Mobile: Y  City: Z"). The inquiry/ticket is created the
# moment all required fields (and, for fraud, the screenshot) are present -- no step-by-step.
#   intro/fields/final, auto_reply, brochure, subject/log_event, attachment_required,
#   creates_ticket/issue_type/priority/phone_field, queue  (see usage in service.py).
_MOBILE = ("mobile", "mobile number", "mobile no", "phone", "phone number", "contact",
           "contact number")

FLOWS = {
    DROPSHIPPING: {
        "intro": "Thank you for your interest in our Dropshipping Program.\n\nPlease reply with "
                 "the following details:\n\n• Full Name\n• Mobile Number\n• City"
                 "\n\nExample:\nName: Chintan Dabhi\nMobile: 7452638014\nCity: Rajkot\n\nOnce we "
                 "receive these details our team will contact you shortly.",
        "fields": [("dropshipping_name", ("name", "full name")),
                   ("dropshipping_mobile", _MOBILE),
                   ("dropshipping_city", ("city",))],
        "final": "Thank you for your interest in our Dropshipping Program.\n\nWe have received "
                 "your details successfully.\n\nOur team will review your inquiry and contact "
                 "you shortly with the next steps.",
    },
    FRANCHISEE: {
        "intro": "Thank you for your interest in our Franchise Program.\n\nPlease reply with the "
                 "following details:\n\n• City\n• Investment Range\n• Mobile "
                 "Number\n\nExample:\nCity: Rajkot\nInvestment: ₹60,000\nMobile: 9876543210"
                 "\n\nOnce we receive these details our team will contact you shortly.",
        "fields": [("franchise_city", ("city",)),
                   ("franchise_investment", ("investment", "investment range", "budget")),
                   ("franchise_mobile", _MOBILE)],
        "final": "Thank you for your interest in our franchise opportunity.\n\nOur team will "
                 "contact you shortly with further details.",
    },
    INVOICE_REQUEST: {
        "intro": "Please reply with the following details:\n\n• Name\n• Mobile Number\n"
                 "• Order Number\n• GST Number\n• Trade Name\n\nExample:\nName: "
                 "Chintan Dabhi\nMobile: 9876543210\nOrder Number: 123456\nGST Number: "
                 "24ABCDE1234F1Z5\nTrade Name: ABC Enterprise\n\nExpected processing time:\n"
                 "Within 24 hours.",
        "fields": [("invoice_name", ("name",)),
                   ("invoice_mobile", _MOBILE),
                   ("invoice_order_number", ("order number", "order no", "order")),
                   ("invoice_gst_number", ("gst number", "gst", "gst no", "gstin")),
                   ("invoice_trade_name", ("trade name", "trade", "business name", "firm name"))],
        "final": "Thank you!\n\nYour invoice request has been submitted successfully.\n\n"
                 "Expected time:\nWithin 24 hours.\n\nIf you need any other assistance, feel "
                 "free to contact us.",
        "queue": "invoice_team",
    },
    BULK_ORDER: {
        "intro": "Thank you for your interest in bulk purchasing.\n\nPlease share the following "
                 "details:\n\n• Name\n• Mobile Number\n• Product Details\n\n"
                 "Example:\nName: Chintan\nMobile: 9876543210\nProduct: SKU123 / 500 Qty",
        "fields": [("bulk_name", ("name",)),
                   ("bulk_mobile", _MOBILE),
                   ("bulk_product_details", ("product details", "product", "products", "sku",
                                             "details", "item"))],
        "final": "Thank you.\n\nYour bulk purchase inquiry has been submitted successfully.\n\n"
                 "Our sales team will contact you shortly.",
    },
    FRAUD_PAYMENT: {
        # The AI has already identified this as payment fraud -> send ONE info-request email
        # straight away (no option menu, no "choose 1 or 2"). Ticket is created after the
        # customer replies with the details + the mandatory payment screenshot.
        "subject": "Additional Information Required – Payment Fraud Report",
        "intro": "Dear Customer,\n\n"
                 "To help us investigate your payment fraud report, please reply to this email "
                 "with the following details:\n\n"
                 "- Brief description of the fraud\n"
                 "- Fraudster's mobile number\n"
                 "- Payment screenshot (Mandatory)\n\n"
                 "Once we receive these details, our support team will investigate your case.\n\n"
                 "Thank you.\n\n"
                 "DeoDap Support Team",
        "fields": [("fraud_description", ("brief description of the fraud", "fraud description",
                                          "description", "fraud", "details")),
                   ("fraud_mobile", ("fraudster's mobile number", "fraudster mobile number",
                                     "fraudster mobile", "fraudster", "fraud mobile") + _MOBILE)],
        "optional_fields": [("reporter_name", ("your full name", "full name", "name")),
                            ("payment_amount", ("payment amount", "amount", "paid amount",
                                                "amount paid"))],
        "attachment_required": True, "attachment_field": "payment_screenshot",
        "final": "Thank you for reporting this.\n\nWe have created your fraud investigation "
                 "ticket.\n\nOur team will review the information and contact you if required.",
        "creates_ticket": True, "issue_type": "FRAUD_PAYMENT", "priority": "high",
        "phone_field": "fraud_mobile",
    },
    FRAUD_ALERT: {
        # AI already identified this as a suspicious-call report -> ONE info-request email (no
        # menu). Collects the customer's registered mobile/email (used to verify them at
        # completion) plus the suspicious caller's number + description in a single reply.
        "subject": "Additional Information Required – Suspicious Call Report",
        "intro": "Dear Customer,\n\n"
                 "To help us investigate your report, please reply to this email with the "
                 "following details:\n\n"
                 "- Registered mobile number\n"
                 "- Registered email address\n"
                 "- Suspicious caller's mobile number\n"
                 "- Brief description of the call or message\n"
                 "- Screenshot of the call log or message (if available)\n\n"
                 "Once we receive these details, our support team will investigate your report."
                 "\n\nThank you.\n\n"
                 "DeoDap Support Team",
        "fields": [("registered_mobile", ("registered mobile number", "registered mobile",
                                          "registered number", "registered mobile no")),
                   ("registered_email", ("registered email address", "registered email",
                                         "email address", "registered mail")),
                   ("suspicious_mobile", ("suspicious caller's mobile number",
                                          "suspicious caller mobile number",
                                          "suspicious caller mobile", "suspicious mobile number",
                                          "suspicious mobile", "caller mobile", "caller number",
                                          "suspicious number", "caller")),
                   ("call_description", ("brief description of the call or message",
                                         "call description", "description", "message details",
                                         "message", "call details", "details"))],
        "optional_fields": [("reporter_name", ("your full name", "full name", "name"))],
        "attachment_required": False, "attachment_field": "screenshot",
        "final": "Thank you for reporting this suspicious activity.\n\nWe have created a fraud "
                 "alert ticket for investigation.",
        "creates_ticket": True, "issue_type": "FRAUD_ALERT", "priority": "high",
        "phone_field": "suspicious_mobile",
    },
    # --- immediate auto-replies (no fields collected) ---
    COMPANY_PROFILE: {
        "auto_reply": True, "brochure": True,
        "subject": "DeoDap Company Profile & Business Information",
        "log_event": "COMPANY_PROFILE_SENT",
        "final": "Thank you for your interest in DeoDap.\n\nPlease find our Company Profile and "
                 "Business Information attached for your reference.\n\nThe brochure contains:\n\n"
                 "• Company Overview\n• Business Model\n• Product Categories\n"
                 "• Franchise Opportunities\n• Dropshipping Information\n• Contact "
                 "Information\n\nIf you require any additional information, please reply to this "
                 "email.\n\nRegards,\nDeoDap Support Team",
    },
    VIP_BULK_PRICING: {
        "auto_reply": True,
        "final": "Visit:\nhttps://deodap.in/pages/vip\n\nCreate your account and submit your "
                 "application.\n\nOnce verified by our team, you will gain access to VIP bulk "
                 "pricing and premium wholesale rates.\n\nThank you.",
    },
    OTHER_INQUIRY: {
        "auto_reply": True,
        "final": "Thank you for contacting DeoDap.\n\nPlease briefly describe your inquiry and "
                 "our team will contact you shortly.",
    },
}


def flow(inquiry_type):
    return FLOWS.get(inquiry_type, {})


def is_auto_reply(inquiry_type):
    return bool(FLOWS.get(inquiry_type, {}).get("auto_reply"))


def flow_fields(inquiry_type):
    return FLOWS.get(inquiry_type, {}).get("fields", [])


def flow_all_fields(inquiry_type):
    """Required + optional fields -- everything we PARSE from the reply (optional fields are
    captured when present but never block ticket creation)."""
    spec = FLOWS.get(inquiry_type, {})
    return list(spec.get("fields", [])) + list(spec.get("optional_fields", []))


def requires_attachment(inquiry_type):
    return bool(FLOWS.get(inquiry_type, {}).get("attachment_required"))


def parse_fields(text, fields):
    """Parse a SINGLE reply of 'Label: value' lines into {field_key: value}. Tolerant of
    order, casing, whitespace and ':' / '-' / '=' separators. Matches the field whose alias
    equals the line label (longest alias first so 'mobile number' beats 'mobile')."""
    parsed = []
    for raw in (text or "").replace("\r", "").split("\n"):
        ln = raw.strip()
        if not ln:
            continue
        for sep in (":", "-", "="):
            if sep in ln:
                label, _, value = ln.partition(sep)
                parsed.append((label.strip().lower(), value.strip()))
                break
    found = {}
    for key, aliases in fields:
        for alias in sorted(aliases, key=len, reverse=True):
            for label, value in parsed:
                if value and label == alias:
                    found[key] = value
                    break
            if key in found:
                break
    return found


def missing_fields(inquiry_type, data):
    """Field keys still missing from `data` for this flow."""
    data = data or {}
    return [key for key, _ in flow_fields(inquiry_type)
            if not str(data.get(key) or "").strip()]


