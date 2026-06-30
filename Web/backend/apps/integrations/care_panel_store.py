"""
DeoDap Care Panel STORE API (care.deodap.in) -- creates the Gallabox ticket and
returns the customer tracking link.

    POST https://care.deodap.in/api/gallabox/ticket/store-json

Auth is a Laravel token (Bearer by default), configured separately from the
care.deodap.info lookup. The exact response shape is NOT assumed: `extract_tracking`
scans the parsed JSON for a tracking URL and a ticket number across every candidate
field the response might use, and as a last resort regex-matches any
`https://care.deodap.in/t?id=...` URL anywhere in the payload.

To capture the REAL response (with a valid token):
    python manage.py probe_store_json --token <care.deodap.in token>
"""

import logging
import re

from django.conf import settings as dj

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20
# store-json sometimes answers with a generic, RETRYABLE error ("Something went wrong,
# Please try again later.") or a 5xx -- a transient server hiccup, not a bad payload.
# Without a retry a single blip leaves the ticket on an internal fallback link forever,
# so we retry a few times with a short backoff before giving up.
STORE_MAX_ATTEMPTS = 3
STORE_RETRY_BACKOFF = 1.5  # seconds, multiplied by the attempt number
_TRANSIENT_MARKERS = ("something went wrong", "try again", "timeout", "timed out",
                      "temporarily", "service unavailable")

# A customer tracking link looks like https://care.deodap.in/t?id=gKp64KxaAz
TRACKING_URL_RE = re.compile(r"https?://[^\s\"']*/t\?id=[A-Za-z0-9_\-]+")
# The store-json response returns only the hash (e.g. "EVPxcdvbvP4"); build the URL.
TRACKING_PAGE_BASE = "https://care.deodap.in/t?id="

# Candidate field names the response MIGHT use (we do not assume one).
TRACKING_FIELDS = [
    "tracking_url", "ticket_url", "public_url", "public_link", "view_url",
    "short_url", "shareable_url", "status_url", "link", "url", "permalink",
]
# Fields that carry just the tracking HASH -> we build the URL from these.
HASH_FIELDS = ["hash", "hash_id", "hashid", "tracking_id", "track_id"]
NUMBER_FIELDS = [
    "ticket_number", "ticket_no", "ticketNumber", "ticket_id", "ticketId",
    "number", "reference", "ref", "id", "code", "slug",
]


def _walk(obj):
    """Yield every (key, value) pair in a nested dict/list structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def extract_tracking(data):
    """Return (tracking_url, ticket_number) found in a parsed response, or ("","").

    URL precedence: an explicit URL field, then a HASH field (build the URL from it,
    e.g. data.hash -> https://care.deodap.in/t?id=<hash>), then a regex match on any
    string. Number: the first matching number field.
    """
    if not isinstance(data, (dict, list)):
        return "", ""

    pairs = list(_walk(data))
    lowered = {k.lower(): v for k, v in pairs if isinstance(k, str)}

    tracking_url = ""
    # 1) An explicit full URL field.
    for field in TRACKING_FIELDS:
        val = lowered.get(field)
        if isinstance(val, str) and val.strip():
            tracking_url = val.strip()
            break
    # 2) Build the URL from a hash field (the store-json shape: data.hash).
    if not tracking_url:
        for field in HASH_FIELDS:
            val = lowered.get(field)
            if isinstance(val, str) and val.strip():
                tracking_url = TRACKING_PAGE_BASE + val.strip()
                break
    # 3) Last resort: any care.deodap.in/t?id= URL anywhere in the response.
    if not tracking_url:
        for _k, v in pairs:
            if isinstance(v, str):
                m = TRACKING_URL_RE.search(v)
                if m:
                    tracking_url = m.group(0)
                    break

    ticket_number = ""
    for field in NUMBER_FIELDS:
        val = lowered.get(field)
        if val not in (None, "", [], {}):
            ticket_number = str(val)
            break

    return tracking_url, ticket_number


class CarePanelStoreClient:
    def __init__(self, url, token, auth="bearer"):
        self.url = url
        self.token = token
        self.auth = (auth or "bearer").lower()

    @property
    def _headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            if self.auth == "x-api-key":
                h["x-api-key"] = self.token
            else:
                h["Authorization"] = f"Bearer {self.token}"
        return h

    def store(self, payload):
        """POST the ticket and return (status_code, parsed_json_or_None, raw_text)."""
        import requests

        r = requests.post(self.url, headers=self._headers, json=payload,
                          timeout=DEFAULT_TIMEOUT)
        try:
            parsed = r.json()
        except ValueError:
            parsed = None
        return r.status_code, parsed, r.text


def build_client():
    url = getattr(dj, "CARE_PANEL_STORE_URL", "")
    token = getattr(dj, "CARE_PANEL_STORE_TOKEN", "")
    if not url or not token:
        return None
    return CarePanelStoreClient(url, token, getattr(dj, "CARE_PANEL_STORE_AUTH", "bearer"))


def _media_payload(ticket):
    """Encode the ticket's photo/video attachments for the store request. The exact
    field name store-json expects is NOT assumed here -- confirm it against the real
    API (probe_store_json) and adjust the key if needed."""
    import base64

    media = []
    for att in ticket.attachments.all():
        ct = (att.content_type or "").lower()
        if not ct.startswith(("image/", "video/")):
            continue
        try:
            with att.file.open("rb") as fh:
                data = fh.read()
        except Exception:  # noqa: BLE001
            logger.exception("Could not read attachment %s for upload", att.id)
            continue
        media.append({
            "filename": att.filename,
            "mime_type": att.content_type,
            "size": att.size,
            "content_base64": base64.b64encode(data).decode("ascii"),
        })
    return media


# Local ticket priority -> Care Panel priority id (1=high, 2=normal, 3=low).
_PRIORITY_MAP = {"high": "1", "normal": "2", "low": "3"}

# Two-step inquiry kind (stored on the ticket as extracted.verify_kind) -> Care Panel
# issue-type NAME (looked up in CARE_PANEL_ISSUE_MAP). Deterministic.
_VERIFY_KIND_ISSUE_TYPE = {
    "invoice": "Invoice Request", "franchise": "Franchise Inquiry",
    "dropship": "Dropshipping Inquiry", "company": "Company Profile Request",
}

# Non-item issue types by keyword (delivered-item sub-types come from the SHARED
# evidence.delivered_item_subtype so a damaged complaint is never mislabelled). Ordered.
_ISSUE_TYPE_KEYWORDS = [
    ("Cancel Order", ("cancel order", "cancel my order", "cancel the order", "want to cancel",
                      "please cancel", "order cancellation")),
    ("Report Fraud", ("fraud", "scam", "fake call", "suspicious call")),
    ("Update Address", ("change address", "update address", "wrong address", "address change")),
    ("Cancelled Delivery", ("rto", "return to origin", "sent back")),
    ("Out For Delivery Issue", ("out for delivery",)),
    ("Reschedule Delivery", ("reschedule", "change delivery date")),
    ("Delayed Delivery", ("delay", "delayed", "late", "not arrived", "still waiting")),
    ("Shipment Tracking", ("track", "tracking", "where is my order", "status of my order")),
    ("Payment Issue", ("payment", "charged", "double charge", "transaction", "deducted",
                       "overcharged", "refund not")),
    ("Refund Status", ("refund",)),
]


# Website / App issue types (the customer's "Website / App Related" group, id 21). LOCKED FIRST
# so an app/website ticket is NEVER mislabelled a Delivery / delivered-item / Other-Items issue.
# NOTE: account sub-topics (Update Phone/Email, OTP, etc.) belong to the ACCOUNT group (id 20),
# not here -- see _ACCOUNT_ISSUE_TYPES.
_WEBSITE_APP_ISSUE_TYPES = (
    "App Crashing / Not Loading", "Cart Not Saving Items", "Checkout Page Not Load",
    "Saved Address Not Found", "Browser & Device Support",
)

# Account Related issue types (the customer's "Account Related" group -> Care Panel id 20). These
# roll up to ONE Gallabox issue (Account Related issues); the SPECIFIC sub-topic is preserved in
# the ticket detail. Checked BEFORE Website/App so OTP / password-reset / contact-update etc. are
# never mislabelled a Website/App (21) issue.
_ACCOUNT_ISSUE_TYPES = (
    "Password Reset Error", "Update Phone / Email", "Delete Account",
    "Data & Privacy Security", "OTP / Notifications Not Received",
    "View Order History", "Create New Account", "Manage Saved Addresses",
)
# Resolve the SPECIFIC account sub-topic from the email text (natural phrasing) -> account name.
_ACCOUNT_SUBTOPIC_KW = (
    ("Password Reset Error", ("password reset", "reset password", "reset my password",
                              "forgot password", "reset link", "can't reset")),
    ("Update Phone / Email", ("update phone", "change phone", "update mobile", "change mobile",
                              "update email", "change email", "update contact", "change my number")),
    ("Delete Account", ("delete account", "delete my account", "close account", "deactivate account",
                        "remove my account")),
    ("Data & Privacy Security", ("data privacy", "privacy", "personal data", "data security",
                                 "account security", "data protection")),
    ("OTP / Notifications Not Received", ("otp", "one time password", "one-time password",
                                          "verification code", "not receiving otp", "otp not",
                                          "notification not received", "notifications not received")),
    ("View Order History", ("order history", "past orders", "previous orders", "my order history")),
    ("Create New Account", ("create account", "create a new account", "new account", "sign up",
                            "signup", "register account", "registration", "can't register")),
    ("Manage Saved Addresses", ("manage saved address", "manage address", "address book",
                                "manage my address", "edit saved address")),
)
# Resolve the SPECIFIC Website/App sub-topic from the email text -- most-specific first; the
# generic app-crash is the fallback. Robust to natural phrasing ('Checkout page stuck').
_WEBSITE_APP_SUBTOPIC_KW = (
    ("Checkout Page Not Load", ("checkout",)),
    ("Cart Not Saving Items", ("cart not", "cart isn", "cart empt", "cart not saving",
                               "cart won", "cart")),
    ("Saved Address Not Found", ("saved address", "address not found", "address not saving",
                                 "saved addresses", "address missing")),
    ("Browser & Device Support", ("browser", "device support", "device compatib",
                                  "not supported", "unsupported", "incompatible")),
    ("App Crashing / Not Loading", ("crash", "not loading", "not load", "not opening",
                                    "won't open", "wont open", "hang", "freez", "stuck",
                                    "not working", "not responding")),
)
# Issue types whose SPECIFIC sub-topic is preserved in the ticket detail ("Sub-topic: ..."),
# because they roll up to ONE shared Gallabox issue (Website/App -> 21, Account -> 20, Offers -> 22).
_SELF_NAMED_ISSUE_TYPES = (set(_WEBSITE_APP_ISSUE_TYPES) | set(_ACCOUNT_ISSUE_TYPES)
                           | {"Ongoing Offers & Sales"})


def _first_inbound_body(ticket):
    """The customer's ORIGINAL words (first inbound message body) -- the ground truth, unaffected
    by an AI mis-summary. Used for payment detection so "₹599 deducted but order not placed" is
    recognised even when the AI labelled the ticket with a generic category / issue_summary."""
    try:
        m = ticket.messages.filter(direction="inbound").order_by("created_at").first()
        return (m.body_text or "") if m else ""
    except Exception:  # noqa: BLE001 -- detection must never break on a message lookup
        return ""


def _detect_issue_type(ticket):
    """Return (issue_type_name, source). Precedence: a verified two-step inquiry; then the
    WEBSITE/APP group (cat 15 + the account sub-topics grouped with it) -- locked so it never
    falls into delivery / item / Other-Items; then the DELIVERED-ITEM sub-type; then other
    non-item keyword issues. None if nothing matches."""
    extracted = ticket.extracted or {}
    kind = extracted.get("verify_kind")
    if kind in _VERIFY_KIND_ISSUE_TYPE:
        return _VERIFY_KIND_ISSUE_TYPE[kind], f"verify_kind:{kind}"

    sub_low = (ticket.sub_topic or "").strip().lower()
    cat_code = (ticket.category or "").split(".")[0].strip()
    ref_code = getattr(getattr(ticket, "category_ref", None), "code", "") or ""
    acct_text = " ".join([ticket.sub_topic or "", ticket.issue_summary or "",
                          extracted.get("issue_summary") or "", ticket.subject or ""]).lower()

    # 0) ACCOUNT group (Care Panel id 20). Checked BEFORE Website/App so OTP / password-reset /
    #    update-contact / delete-account etc. are NEVER mislabelled a Website/App (21) issue.
    for name in _ACCOUNT_ISSUE_TYPES:
        if name.lower() in sub_low:
            return name, "account"
    for name, kws in _ACCOUNT_SUBTOPIC_KW:
        if any(k in acct_text for k in kws):
            return name, "account_text"

    # 1) Exact Website/App sub-topic name on the ticket.
    for name in _WEBSITE_APP_ISSUE_TYPES:
        if name.lower() in sub_low:
            return name, "website_app"
    # 2) A Website/App (cat 15) ticket -> resolve the SPECIFIC sub-topic from the text keywords,
    #    so it is NEVER a generic / delivery / Other-Items fallback.
    if cat_code == "15" or ref_code == "15":
        wa_text = " ".join([ticket.sub_topic or "", ticket.issue_summary or "",
                            extracted.get("issue_summary") or "", ticket.subject or ""]).lower()
        for name, kws in _WEBSITE_APP_SUBTOPIC_KW:
            if any(k in wa_text for k in kws):
                return name, "website_app_text"
        return "App Crashing / Not Loading", "website_app_default"

    # Offers / discounts / coupons -> Ongoing Offers & Sales (never a delivery / item issue).
    if cat_code == "10" or ref_code == "10" or "offer" in sub_low or "discount" in sub_low \
            or "ongoing offers" in sub_low:
        return "Ongoing Offers & Sales", "offers"

    # Payment & Invoice (cat 8) -- LOCKED so a "payment deducted but order not placed" / charged /
    # transaction issue is NEVER mislabelled a delivery / delivered-item / Other issue. Maps to
    # Gallabox 6 "payment issue". Detected deterministically (matches the evidence-flow's payment
    # detection) BEFORE the delivered-item / keyword fallbacks. We read the CUSTOMER'S ORIGINAL
    # message body too -- the reported bug was a payment email whose AI category/issue_summary were
    # generic ("Other Delivery Related Issue"), so a field-only check missed it; the customer's own
    # words ("₹599 deducted but order not placed") are unambiguous. Pure invoice requests arrive via
    # verify_kind above, so a cat-8 ticket reaching here with a payment keyword is a real payment
    # problem.
    from apps.decision import policy
    body_text = _first_inbound_body(ticket)
    pay_text = " ".join([ticket.sub_topic or "", ticket.issue_summary or "",
                         extracted.get("issue_summary") or "", ticket.subject or "", body_text])
    pay_low = pay_text.lower()
    # "Payment deducted but order NOT placed" = money taken with no order -> CyberFraud Report
    # (issue 12) per the brand. Detected from the CUSTOMER'S OWN words (body), so a generic AI
    # category never sends it to an Other/Delivery default. Checked first / most specific.
    if policy.payment_no_order(pay_text) or "payment deducted but order not placed" in pay_low \
            or "payment deducted" in sub_low:
        return "Payment Fraud", "payment_no_order"
    # Other payment problems (double charge / overcharged / generic Payment & Invoice).
    if "payment issue" in sub_low \
            or (extracted.get("verify_kind") in (None, "") and "payment" in sub_low) \
            or ((cat_code == "8" or ref_code == "8")
                and any(w in pay_low for w in ("payment", "deducted", "debited", "charged",
                                               "transaction", "double charge", "overcharged"))):
        return "Payment Issue", "payment"

    from apps.ingestion import evidence

    text = " ".join([ticket.sub_topic or "", ticket.issue_summary or "",
                     extracted.get("issue_summary") or "", ticket.subject or "",
                     ticket.category or ""])
    # "Order Shown Delivered But Not Received" (Care Panel issue 3) -- BEFORE the delivered-
    # item sub-type so "delivered but not received" is never mislabelled 'Missing Item' (7).
    if evidence.is_delivered_not_received(text):
        return "Undelivered Issue", "delivered_not_received"
    subtype = evidence.delivered_item_subtype(text)   # Damaged Item / Missing Item / ...
    if subtype:
        return subtype, f"subtype:{subtype}"
    low = text.lower()
    for issue_type, keywords in _ISSUE_TYPE_KEYWORDS:
        if any(k in low for k in keywords):
            return issue_type, f"keyword:{issue_type}"
    return None, "none"


def resolve_issue(ticket):
    """Resolve (issue_id, issue_name, source) against the REAL Care Panel issue ids.

    Precedence: detected issue-type (verify_kind -> delivered-item sub-type -> keyword)
    mapped via CARE_PANEL_ISSUE_MAP -> default. The issue_id is the SPECIFIC item issue
    (Damaged->8, etc.), NEVER our broad category code. Logs AI-CATEGORY / AI-SUBCATEGORY /
    ISSUE-MAPPING-SOURCE / FINAL-ISSUE."""
    issue_map = getattr(dj, "CARE_PANEL_ISSUE_MAP", {}) or {}
    id_to_name = getattr(dj, "CARE_PANEL_ISSUE_IDS", {}) or {}
    default_id = str(getattr(dj, "CARE_PANEL_DEFAULT_ISSUE_ID", "6"))

    # Logged right BEFORE Care Panel ticket creation -- the classifier category/sub-topic and
    # the FINAL (locked) category/sub-topic are the SAME here: no reclassification happens on
    # the way to the Care Panel, so a Category-15 ticket stays Category 15.
    logger.info("CLASSIFIER-CATEGORY %s", ticket.category or "-")
    logger.info("CLASSIFIER-SUBTOPIC %s", ticket.sub_topic or "-")
    logger.info("AI-CATEGORY %s", ticket.category or "-")
    logger.info("AI-SUBCATEGORY %s", ticket.sub_topic or "-")

    issue_type, type_source = _detect_issue_type(ticket)
    if issue_type and issue_type in issue_map:
        issue_id, source = str(issue_map[issue_type]), type_source
    else:
        issue_id, source = default_id, "default"

    # The store-json API REQUIRES a numeric issue id; a non-numeric value (e.g. the sub-topic
    # code "15.1") is rejected with a 400 -> NO data.hash -> NO tracking link. Coerce any
    # non-numeric id to the configured numeric Website/App id so the link is ALWAYS created.
    # Set CARE_PANEL_*_ISSUE_ID to the Care Panel's REAL numeric ids to also show the exact name.
    if not issue_id.lstrip("-").isdigit():
        fallback = str(getattr(dj, "CARE_PANEL_WEBSITE_APP_ISSUE_ID", default_id))
        logger.warning("ISSUE-ID-NONNUMERIC %r (issue_type=%s) -> numeric fallback %s so the "
                       "tracking link is created. Set CARE_PANEL_*_ISSUE_ID to the real Care "
                       "Panel id to show the exact name.", issue_id, issue_type, fallback)
        issue_id, source = fallback, f"{source}:nonnumeric_fallback"

    # The issue NAME is the Gallabox catalog label for the id we send: Website/App sub-topics ->
    # id 21 -> "Website/App Related issues"; offers -> 22; delivery/item/fraud -> their own label.
    # The SPECIFIC sub-topic is preserved in the ticket detail ("Sub-topic: ...").
    issue_name = id_to_name.get(str(issue_id)) or issue_type or ""
    logger.info("FINAL-CATEGORY %s", ticket.category or "-")
    logger.info("FINAL-SUBTOPIC %s", ticket.sub_topic or "-")
    logger.info("ISSUE-MAPPING-SOURCE %s (issue_type=%s)", source, issue_type or "-")
    # --- temp trace logs (WEBSITE_APP_MAPPING / ISSUE_ID_SELECTED) ---
    if issue_type in _SELF_NAMED_ISSUE_TYPES:
        logger.info("WEBSITE_APP_MAPPING sub_topic=%r -> issue_type=%r mapped_id=%r (Gallabox %r)",
                    ticket.sub_topic or "-", issue_type, issue_id,
                    id_to_name.get(str(issue_id), "?"))
    logger.info("ISSUE_ID_SELECTED=%s source=%s issue_type=%s", issue_id, source,
                issue_type or "-")
    logger.info("CLASSIFICATION_SUBTOPIC=%s", issue_type or ticket.sub_topic or "-")
    logger.info("CARE_PANEL_ISSUE=%s", issue_name or "-")
    logger.info("CARE_PANEL_ISSUE_ID=%s", issue_id)
    logger.info("CARE_PANEL_ISSUE_NAME=%s", issue_name or "-")
    logger.info("FINAL_TICKET_ISSUE=%s", issue_name or "-")
    # The numeric id we SEND vs the Care Panel's catalog NAME for that id: when they differ the
    # panel's "Issue" dropdown shows its own label until CARE_PANEL_*_ISSUE_ID is the real id.
    panel_label = id_to_name.get(str(issue_id))
    if panel_label and panel_label != issue_name:
        logger.warning("CARE_PANEL_PANEL_LABEL=%s (id=%s) != FINAL_TICKET_ISSUE=%s -- set the "
                       "real Care Panel issue id via CARE_PANEL_*_ISSUE_ID so the panel shows "
                       "'%s'.", panel_label, issue_id, issue_name, issue_name)
    return issue_id, issue_name, source


def _issue_id_for(ticket):
    return resolve_issue(ticket)[0]


def _customer_name(ticket):
    """The Care Panel ticket's customer name. ONLY the VERIFIED Shopify customer name (the
    order owner, stamped at verification with customer_name_source='shopify_verified') is
    ever used. The email sender's display name is NEVER used. When Shopify verification did
    not yield a name, the customer name is left blank ('Unknown') -- never the sender."""
    extracted = ticket.extracted or {}
    name = extracted.get("customer_name")
    if name and extracted.get("customer_name_source") == "shopify_verified":
        logger.info("CUSTOMER-NAME-SOURCE shopify_verified -> TICKET-CUSTOMER-NAME %s", name)
        return name
    logger.info("CUSTOMER-NAME-SOURCE none (not Shopify-verified) -> TICKET-CUSTOMER-NAME "
                "Unknown (email sender deliberately NOT used).")
    return "Unknown"


def _store_phone(raw):
    """store-json requires phone <= 10 chars: the BARE 10-digit Indian mobile (NO +91).
    Shopify returns E.164 ('+919847505805'), which the API rejects with a 400
    ('The phone field must not be greater than 10 characters.') -> no data.hash, no link."""
    try:
        from apps.classifier.rule_classifier import normalize_phone
        n = normalize_phone(raw)
        if n:
            return n
    except Exception:  # noqa: BLE001
        pass
    digits = "".join(c for c in str(raw or "") if c.isdigit())
    return digits[-10:] if len(digits) > 10 else digits


def _payload(ticket, include_media=False):
    """Build the store-json request body in the REAL schema (verified live):
    order_no / detail / issue_id / priority. Media is NOT sent here -- it's uploaded
    separately via the tracking-page comment form (care_panel_media)."""
    extracted = ticket.extracted or {}
    issue_id, issue_name, _src = resolve_issue(ticket)
    specific_subtopic, _src2 = _detect_issue_type(ticket)
    detail = ticket.issue_summary or extracted.get("issue_summary") or ticket.subject or ""
    # All Website/App sub-topics share ONE generic Care Panel issue, so the SPECIFIC sub-topic is
    # preserved in the detail ("Sub-topic: App Crashing / Not Loading"); offers are surfaced the
    # same way. The agent always sees the true sub-topic even though the dropdown is generic.
    if specific_subtopic and specific_subtopic in _SELF_NAMED_ISSUE_TYPES:
        detail = f"Sub-topic: {specific_subtopic}\n\n{detail}".strip()
    return {
        "source_id": str(getattr(dj, "CARE_PANEL_STORE_SOURCE_ID", "3")),
        "name": _customer_name(ticket),
        "phone": _store_phone(extracted.get("phone") or ""),
        # ORDER OWNER email (verified Shopify order) -> falls back to the sender's address
        # only when no order was verified.
        "email": extracted.get("customer_email") or ticket.customer_email or "",
        "order_no": extracted.get("order_id") or "",
        "awb": extracted.get("awb") or "",
        "courier": extracted.get("courier") or "",
        "tracking_url": "",
        "issue_id": issue_id,        # real Gallabox id (21 Website/App, 22 Offers, etc.)
        # The displayed issue NAME (matches the id's catalog label) -- belt-and-braces in case
        # the panel renders a name field rather than re-looking-up the id.
        "issue": issue_name,
        "issue_name": issue_name,
        "priority": _PRIORITY_MAP.get(ticket.priority, "2"),
        "detail": detail,
    }


def _store_returned_media_urls(ticket, parsed):
    """Save any media URLs the store response returned onto the ticket's attachments
    (so the local record links to the Care Panel copy). Field-agnostic."""
    urls = []
    for _k, v in _walk(parsed):
        if isinstance(v, str) and v.startswith(("http://", "https://")) and \
                any(v.lower().endswith(e) for e in
                    (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm")):
            urls.append(v)
    if not urls:
        return
    attachments = list(ticket.attachments.filter(remote_url=""))
    for att, url in zip(attachments, urls):
        att.remote_url = url
        att.save(update_fields=["remote_url", "updated_at"])
    logger.info("Care Panel %s: stored %d returned media URL(s)", ticket.ticket_id, len(urls))


def _response_ok(status, parsed):
    """True when store-json confirms the ticket was created."""
    if status >= 400 or parsed is None:
        return False
    if isinstance(parsed, dict) and "success" in parsed:
        return str(parsed.get("success", "")).lower() in ("success", "true", "1")
    return bool(parsed)  # no explicit success flag -> a non-empty body means OK


def _is_transient(status, parsed, raw):
    """True when a failed response is a RETRYABLE server hiccup (not a bad payload).

    store-json returns HTTP 200 with {"success":"failed","message":"Something went
    wrong, Please try again later."} on transient errors; 5xx is also retryable.
    A 4xx validation error (e.g. "The phone field is required.") is NOT."""
    if status >= 500:
        return True
    blob = (raw or "").lower()
    if isinstance(parsed, dict):
        blob += " " + str(parsed.get("message", "")).lower()
    return any(m in blob for m in _TRANSIENT_MARKERS)


def store_ticket(ticket, client=None):
    """Create the ticket in the Care Panel (with media) and save its tracking link +
    number + returned media URLs. Best-effort; no-op when not configured.

    Logs the ticket-creation/upload response in full and the exact API error on
    failure (per spec). Returns the tracking URL.
    """
    from apps.tickets.models import AuditLogEntry

    if client is None:
        client = build_client()
    if client is None:
        logger.info("Care Panel store skipped for %s: store API not configured "
                    "(set CARE_PANEL_STORE_TOKEN).", ticket.ticket_id)
        return ""

    import json as _json

    payload = _payload(ticket)
    logger.info("LOCAL-TICKET-ID=%s", ticket.ticket_id)
    logger.info("CARE-PANEL-CREATE-REQUEST=%s", _json.dumps(payload, ensure_ascii=False)[:1000])
    logger.info("CARE_PANEL_PAYLOAD=%s", _json.dumps(payload, ensure_ascii=False)[:1000])
    logger.info("CARE_PANEL_REQUEST ticket=%s url=%s payload=%s", ticket.ticket_id,
                getattr(client, "url", "?"), _json.dumps(payload, ensure_ascii=False)[:1000])

    # store-json is PHONE-KEYED ("The phone field is required."). Without a phone the
    # API returns 400 and no tracking link can be generated -- fail fast + clearly so
    # the cause is obvious in the logs (the #1 reason a confirmation has no link).
    if not payload.get("phone"):
        logger.error("CARE_PANEL_SKIPPED ticket=%s reason=no_phone -> store-json NOT "
                     "called (phone-keyed); NO data.hash, NO care.deodap.in link created.",
                     ticket.ticket_id)
        AuditLogEntry.objects.create(
            ticket=ticket, actor="system", event="care_panel_store_failed",
            detail={"reason": "no_phone"},
        )
        return ""

    import time

    max_attempts = max(1, int(getattr(dj, "CARE_PANEL_STORE_MAX_ATTEMPTS", STORE_MAX_ATTEMPTS)))
    backoff = float(getattr(dj, "CARE_PANEL_STORE_RETRY_BACKOFF", STORE_RETRY_BACKOFF))
    status, parsed, raw = None, None, ""
    for attempt in range(1, max_attempts + 1):
        try:
            status, parsed, raw = client.store(payload)
        except Exception as exc:  # noqa: BLE001 -- best-effort; network errors are transient
            logger.warning("Care Panel store EXCEPTION for %s (attempt %d/%d): %s",
                           ticket.ticket_id, attempt, max_attempts, exc)
            status, parsed, raw = None, None, str(exc)
            if attempt < max_attempts:
                time.sleep(backoff * attempt)
                continue
            logger.exception("Care Panel store EXCEPTION for %s (final)", ticket.ticket_id)
            AuditLogEntry.objects.create(
                ticket=ticket, actor="system", event="care_panel_store_failed",
                detail={"error": (raw or "")[:300], "attempts": attempt},
            )
            return ""

        # --- Ticket creation / attachment upload response (logged in full) ---
        logger.info("CARE-PANEL-CREATE-RESPONSE=status=%s body=%s", status, (raw or "")[:1000])
        logger.info("CARE_PANEL_RESPONSE ticket=%s status=%s attempt=%d body=%s",
                    ticket.ticket_id, status, attempt, (raw or "")[:1000])

        if _response_ok(status, parsed):
            break

        # Failed. Retry ONLY transient hiccups ("Something went wrong" / 5xx); a real
        # validation error (bad payload) will never succeed, so fail fast on it.
        if attempt < max_attempts and _is_transient(status, parsed, raw):
            logger.warning("CARE_PANEL_STORE_TRANSIENT ticket=%s status=%s attempt=%d/%d "
                           "-> retrying: %s", ticket.ticket_id, status, attempt,
                           max_attempts, (raw or "")[:200])
            time.sleep(backoff * attempt)
            continue

        # Exact API error (per spec).
        logger.error("CARE_PANEL_CREATE_FAILED ticket=%s status=%s attempts=%d error=%s",
                     ticket.ticket_id, status, attempt, (raw or "")[:500])
        AuditLogEntry.objects.create(
            ticket=ticket, actor="system", event="care_panel_store_failed",
            detail={"status": status, "body": (raw or "")[:500], "attempts": attempt,
                    "transient": _is_transient(status, parsed, raw)},
        )
        return ""

    tracking_url, ticket_number = extract_tracking(parsed)
    hash_id = tracking_url.split("id=")[-1] if "id=" in tracking_url else ""
    if hash_id:
        logger.info("CARE_PANEL_HASH_RECEIVED ticket=%s data.hash=%s ticket_number=%s",
                    ticket.ticket_id, hash_id, ticket_number)
    else:
        logger.warning("Care Panel created ticket=%s but NO data.hash in response -> no "
                       "resolvable care.deodap.in link.", ticket.ticket_id)

    updates = []
    if tracking_url:
        ticket.tracking_url = tracking_url
        updates.append("tracking_url")
        logger.info("TRACKING_URL_SAVED ticket=%s tracking_url=%s (from data.hash=%s)",
                    ticket.ticket_id, tracking_url, hash_id)
    if ticket_number:
        ticket.ticket_number = ticket_number
        updates.append("ticket_number")
    if hash_id:
        # Store the hash as the care_panel_ticket_id so media upload (which keys on
        # the tracking-page hashId) works for newly-created tickets too. Clear the
        # internal-tracking flag -- this ticket now has a REAL care.deodap.in link.
        new_extracted = {**(ticket.extracted or {}), "care_panel_ticket_id": hash_id}
        new_extracted.pop("internal_tracking", None)
        ticket.extracted = new_extracted
        updates.append("extracted")
    if updates:
        ticket.save(update_fields=[*updates, "updated_at"])
    _store_returned_media_urls(ticket, parsed)

    logger.info("Care Panel STORED ticket=%s tracking_url=%s number=%s",
                ticket.ticket_id, tracking_url, ticket_number)
    AuditLogEntry.objects.create(
        ticket=ticket, actor="system", event="care_panel_stored",
        detail={"tracking_url": tracking_url, "ticket_number": ticket_number,
                "status": status},
    )
    return tracking_url
