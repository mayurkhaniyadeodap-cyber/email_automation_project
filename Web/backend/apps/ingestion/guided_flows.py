"""
Sub-topic guided flows for Website / App (cat 15) and Account (cat 14) sub-topics.

Uniform shape for every category:  AUTO-VERIFY  ->  consolidated single-reply COLLECT  ->
ticket (or guided auto-reply). State lives on a PendingConversation under
`extracted['guided_flow']` (+ `step` index + collected `guided_data`).

AUTO-VERIFICATION RULE (applies to ALL categories): identifiers (Order Number / Registered
Mobile / Registered Email) are extracted from the FIRST message and verified automatically --
the customer is NEVER asked to verify again when the first message already carried one. Only
when nothing is found do we ask; only on a real Shopify NO-MATCH do we say "could not verify".

    App Crashing / Cart / Checkout  -> verify -> [issue + screenshot + video]      -> TICKET
    Update Phone / Email            -> verify -> [new mobile + new email]           -> TICKET
    OTP / Notifications Not Recvd   -> verify -> [registered email + mobile]        -> TICKET
    Delete Account                  -> verify -> [email + mobile + reason]          -> AUTO-REPLY
    Data & Privacy Security         -> verify -> [describe concern]                 -> AUTO-REPLY

Logic that touches Shopify / evidence / ticket creation is reused from `service` via late
imports (service imports this module in its gate, so a top-level import would be circular).
"""
import logging
import re

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Customer-facing wording.
# --------------------------------------------------------------------------- #
VERIFY_PROMPT = ("Please provide any ONE of the following:\n"
                 "• Order Number\n"
                 "OR\n"
                 "• Registered Email ID\n"
                 "OR\n"
                 "• Registered Mobile Number")
VERIFY_OK = "Verification successful."
VERIFY_FAIL = ("We could not verify the provided information.\n\n"
               "Please reply with a valid:\n"
               "• Order Number\n"
               "• Registered Email ID\n"
               "• Registered Mobile Number")

MEDIA_DETAILS_PROMPT = ("Please provide ALL details in ONE reply:\n"
                        "• Issue Description\n"
                        "• Screenshot\n"
                        "• Video")
UPDATE_CONTACT_PROMPT = ("Please provide ALL details in ONE reply:\n"
                         "• New Mobile Number\n"
                         "• New Email ID")
OTP_PROMPT = ("Please provide ALL details in ONE reply:\n"
              "• Registered Email ID\n"
              "• Registered Mobile Number")
DELETE_PROMPT = ("Please provide ALL details in ONE reply:\n"
                 "• Registered Email ID\n"
                 "• Registered Mobile Number\n"
                 "• Reason for Account Deletion")
PRIVACY_PROMPT = "Please describe your concern regarding data privacy or security."

DELETE_CONFIRM = (
    "Your account deletion request has been submitted successfully.\n\n"
    "Our team will review your request and contact you if required.")
PRIVACY_INFO = (
    "We use secure payment gateways and do not store card details on our servers.\n\n"
    "Your information is handled according to our Privacy Policy.")

# Make Changes To Order -- EXACT wording (subjects + bodies) per the workflow spec.
MCO_VERIFY_FAIL_SUBJECT = "Verification Required"
MCO_VERIFY_FAIL = (
    "We could not verify your order details.\n\n"
    "Please reply with any one of the following:\n\n"
    "• Order Number\n• Registered Email ID\n• Registered Mobile Number\n\n"
    "Once we receive the information, we will verify your order and assist you further.\n\n"
    "Regards,\nDeoDap Support Team")

ADDRESS_PROMPT_SUBJECT = "Address / Phone Update Request"
ADDRESS_DETAILS_PROMPT = (
    "To process your request, please reply with:\n\n"
    "• Complete New Address\n• New Mobile Number\n\n"
    "Regards,\nDeoDap Support Team")
ADDRESS_TICKET_SUBJECT = "Address / Phone Update Request Received"
ADDRESS_TICKET_BODY = (
    "We have received your updated address/mobile details.\n\n"
    "Please note that if the order has already been shipped, pincode changes may not be "
    "possible. Our team will create a ticket and coordinate with the courier wherever "
    "feasible.\n\nRegards,\nDeoDap Support Team")

ITEMS_SUBJECT = "Order Modification Request"
ITEMS_CONFIRM_PROMPT = (
    "Please confirm whether you would like to add items or update existing items in your "
    "order.\n\nRegards,\nDeoDap Support Team")
ADD_ITEMS_REPLY = (
    "Additional items cannot be added to an existing order. Please place a new order for the "
    "required items.\n\nRegards,\nDeoDap Support Team")

GST_SUBJECT = "GST Details Update Request"
GST_REPLY = (
    "Please note that GSTIN updates are not allowed once the order has been confirmed.\n\n"
    "Regards,\nDeoDap Support Team")

# Ongoing Offers & Sales -- auto-reply only (general inquiry vs discount problem).
OFFERS_SUBJECT = "Ongoing Offers & Sales"
OFFERS_GENERAL_REPLY = (
    "Thank you for contacting DeoDap.\n\n"
    "Please let us know the offer, discount, coupon, or promotion you are referring to so we "
    "can assist you better.\n\nRegards,\nDeoDap Support Team")
OFFERS_PROBLEM_REPLY = (
    "Thank you for contacting DeoDap.\n\n"
    "To help us investigate the issue, please share:\n\n"
    "• Clear screenshot of the discount problem\n"
    "• Offer/Coupon name\n\nRegards,\nDeoDap Support Team")


# --------------------------------------------------------------------------- #
# Flow specification. A flow is [verify step, collect step] + terminal.
# --------------------------------------------------------------------------- #
def _item(key, type_, label):
    return {"key": key, "type": type_, "label": label}


ISSUE = _item("issue", "issue", "Issue Description")
PHOTO = _item("photo", "photo", "Screenshot")
VIDEO = _item("video", "video", "Video")
MEDIA_ITEMS = [ISSUE, PHOTO, VIDEO]


def _verify():
    return {"kind": "verify"}


def _collect(items, prompt):
    return {"kind": "collect", "items": items, "prompt": prompt}


FLOWS = {
    "app_crash": {
        "label": "App Crashing / Not Loading",
        "steps": [_verify(), _collect(MEDIA_ITEMS, MEDIA_DETAILS_PROMPT)],
        "terminal": "ticket",
    },
    "cart_not_saving": {
        "label": "Cart Not Saving Items",
        "steps": [_verify(), _collect(MEDIA_ITEMS, MEDIA_DETAILS_PROMPT)],
        "terminal": "ticket",
    },
    "checkout_not_load": {
        "label": "Checkout Page Not Load",
        "steps": [_verify(), _collect(MEDIA_ITEMS, MEDIA_DETAILS_PROMPT)],
        "terminal": "ticket",
    },
    "update_contact": {
        "label": "Update Phone / Email",
        "steps": [_verify(), _collect(
            [_item("new_phone", "phone", "New Mobile Number"),
             _item("new_email", "email", "New Email ID")], UPDATE_CONTACT_PROMPT)],
        "terminal": "ticket",
    },
    "otp_not_received": {
        "label": "OTP / Notifications Not Received",
        "steps": [_verify(), _collect(
            [_item("email", "email", "Registered Email ID"),
             _item("mobile", "phone", "Registered Mobile Number")], OTP_PROMPT)],
        "terminal": "ticket",
    },
    "delete_account": {
        "label": "Delete Account",
        "steps": [_verify(), _collect(
            [_item("email", "email", "Registered Email ID"),
             _item("mobile", "phone", "Registered Mobile Number"),
             _item("reason", "text", "Reason for Account Deletion")], DELETE_PROMPT)],
        "terminal": "auto_reply", "reply": DELETE_CONFIRM,
    },
    "data_privacy": {
        "label": "Data & Privacy Security",
        "steps": [_verify(), _collect([_item("concern", "text", "your concern")],
                                      PRIVACY_PROMPT)],
        "terminal": "auto_reply", "reply": PRIVACY_INFO,
    },
    # --- Make Changes To Order (deterministic wording + subjects) ---
    "update_address": {                         # verify -> new address + mobile -> TICKET
        "label": "Update Address / Phone",
        "steps": [_verify(), _collect(
            [_item("new_address", "text", "New Address"),
             _item("new_mobile", "mobile", "New Mobile Number")], ADDRESS_DETAILS_PROMPT)],
        "terminal": "ticket",
        "verify_fail": (MCO_VERIFY_FAIL_SUBJECT, MCO_VERIFY_FAIL),
        "collect_subject": ADDRESS_PROMPT_SUBJECT,
        "ticket_confirmation": (ADDRESS_TICKET_SUBJECT, ADDRESS_TICKET_BODY),
    },
    "add_items": {                              # immediate auto-reply (no confirm), NO ticket
        "label": "Add / Update Items",
        "steps": [], "terminal": "auto_reply", "reply": ADD_ITEMS_REPLY,
        "reply_subject": ITEMS_SUBJECT,
    },
    "gst_update": {                             # verify -> auto-reply (NO ticket)
        "label": "Add / Update GST Details",
        "steps": [_verify()],
        "terminal": "auto_reply", "reply": GST_REPLY,
        "verify_fail": (MCO_VERIFY_FAIL_SUBJECT, MCO_VERIFY_FAIL),
        "reply_subject": GST_SUBJECT,
    },
    # --- Ongoing Offers & Sales (no verification, immediate auto-reply, NO ticket) ---
    "offers_general": {
        "label": "Ongoing Offers & Sales",
        "steps": [], "terminal": "auto_reply", "reply": OFFERS_GENERAL_REPLY,
        "reply_subject": OFFERS_SUBJECT,
    },
    "offers_problem": {
        "label": "Ongoing Offers & Sales",
        "steps": [], "terminal": "auto_reply", "reply": OFFERS_PROBLEM_REPLY,
        "reply_subject": OFFERS_SUBJECT,
    },
}


# --------------------------------------------------------------------------- #
# Flow detection from the classified result. Cat 15 is reliable (the classifier's
# website/app override forces it); cat 14 sub-topics drive the account flows.
# --------------------------------------------------------------------------- #
def _has(blob, kws):
    return any(k in blob for k in kws)


_CHECKOUT_KW = ("checkout page not load", "checkout page not loading", "checkout not loading",
                "checkout not working", "checkout page", "checkout error")
_BROWSER_KW = ("browser", "device compatibility", "device support", "incompatible")
_CART_KW = ("cart not saving", "cart not updating", "cart empties", "cart not working",
            "cart not saving items")
_DELETE_KW = ("delete account", "delete my account", "close account", "deactivate account",
              "delete acc")
_PRIVACY_KW = ("data & privacy", "data and privacy", "data privacy", "privacy security",
               "data security", "privacy & security", "privacy and security", "privacy policy")
_OTP_KW = ("otp", "one time password", "notification not received", "notifications not received",
           "not receiving otp", "not getting otp")
_UPDATE_CONTACT_KW = ("update phone", "update email", "update phone / email", "update my phone",
                      "update my email", "change phone", "change email", "new mobile number",
                      "new email", "update contact")
# Make Changes To Order (cat 5): items vs GST.
_GST_KW = ("gst", "gstin", "tax invoice detail", "tax detail", "gst detail", "gst number")
_ITEMS_KW = ("add item", "update item", "add items", "update items", "add / update item",
             "add product", "add more item", "include item", "extra item")


def detect_flow(result, message):
    """Return the guided-flow key for this classified email, or None. Restricted to the
    Website/App (15) and Account (14) categories so it never hijacks delivery/order mail."""
    if result is None or not getattr(result, "is_support_request", False):
        return None
    from apps.decision import policy

    code = (getattr(result, "category", "") or "").split(".")[0].strip()
    sub = (getattr(result, "sub_topic", "") or "")
    blob = (f"{sub} {message.get('subject','') or ''} "
            f"{message.get('body_text') or message.get('snippet') or ''}").lower()

    # Add/Update Items & GST -> ALWAYS the no-ticket auto-reply flow, even when the classifier
    # didn't tag them cat 5 ("Add one more item to my order" / "Add and update GST details").
    nt = policy.no_ticket_flow(blob)
    if nt:
        return nt

    # Offers / discounts / coupons -> auto-reply (Ongoing Offers & Sales), never a delivery
    # issue. General inquiry vs discount problem -- split on the EMAIL TEXT only (not the
    # classified sub-topic, whose generic 'Issue' would falsely trip the problem branch).
    of = policy.offers_flow(f"{message.get('subject','') or ''} "
                            f"{message.get('body_text') or message.get('snippet') or ''}")
    if of:
        return of

    if code == "15":
        if _has(blob, _CHECKOUT_KW) or _has(blob, _BROWSER_KW):
            return "checkout_not_load"
        if _has(blob, _CART_KW):
            return "cart_not_saving"
        return "app_crash"                       # default cat-15 fault -> app crashing
    if code == "2":                              # Delivery Address & Customer Info Changes
        return "update_address"
    if code == "5":                              # Order Placement & Modification
        if _has(blob, _GST_KW):
            return "gst_update"
        if _has(blob, _ITEMS_KW):
            return "add_items"
        return None                              # other cat-5 (place order) -> not guided
    if code == "14":
        if _has(blob, _DELETE_KW):
            return "delete_account"
        if _has(blob, _PRIVACY_KW):
            return "data_privacy"
        if _has(blob, _OTP_KW):
            return "otp_not_received"
        if _has(blob, _UPDATE_CONTACT_KW):
            return "update_contact"
    return None


# --------------------------------------------------------------------------- #
# Reply parsing helpers.
# --------------------------------------------------------------------------- #
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _extract_email(text):
    m = _EMAIL_RE.search(text or "")
    return m.group(0) if m else ""


_PHONE_RUN_RE = re.compile(r"\d[\d\s\-]{7,}\d")


def _extract_phone(text):
    """First user-entered contact/mobile number in the text -> bare 10 digits (strips +91 / 91
    / 0). Scans digit RUNS (not all digits globally) so a number sitting next to an email like
    'example123@gmail.com' is parsed correctly. Lenient on the first digit -- a NEW number a
    customer sets need not match the Indian-mobile heuristic."""
    for run in _PHONE_RUN_RE.findall(text or ""):
        digits = re.sub(r"\D", "", run)
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if len(digits) == 10:
            return digits
    return ""


def _send(pending, body, subject=None):
    from apps.ingestion import service
    service._inquiry_send(pending, body, subject=subject)
    pending.evidence_requests = (pending.evidence_requests or 0) + 1
    pending.save(update_fields=["evidence_requests", "last_message_id", "updated_at"])


# --------------------------------------------------------------------------- #
# Flow runner.
# --------------------------------------------------------------------------- #
def start_flow(mailbox, message, result, flow_key):
    """First inbound email classified into a guided sub-topic -> open the flow and AUTO-VERIFY
    from this same message (never ask to verify when an identifier is already present)."""
    from apps.ingestion import service

    flow = FLOWS[flow_key]
    logger.info("GUIDED-FLOW-START flow=%s label=%r from=%s", flow_key, flow["label"],
                message.get("from_email"))
    pending = service._create_pending(mailbox, message, result, status="awaiting_evidence")
    ex = dict(pending.extracted or {})
    ex["guided_flow"] = flow_key
    ex["step"] = 0
    # Pre-capture the issue description from the FIRST email (it usually already states it).
    first_text = (message.get("body_text") or message.get("snippet") or "").strip()
    ex["guided_data"] = {"issue": first_text} if len(first_text) >= 4 else {}
    pending.extracted = ex
    pending.save(update_fields=["extracted", "updated_at"])
    return _run_step(mailbox, message, pending, flow, ex)


def handle_reply(mailbox, message, pending):
    """A reply within an active guided flow -> process the current step."""
    ex = dict(pending.extracted or {})
    flow = FLOWS.get(ex.get("guided_flow"))
    if flow is None:                                    # unknown flow -> let normal gates run
        return None, None, True
    return _run_step(mailbox, message, pending, flow, ex)


def _run_step(mailbox, message, pending, flow, ex):
    steps = flow["steps"]
    idx = ex.get("step", 0)
    if idx >= len(steps):
        return _finish(mailbox, message, pending, flow, ex)
    step = steps[idx]
    if step["kind"] == "verify":
        return _do_verify(mailbox, message, pending, flow, ex, idx)
    if step["kind"] == "collect":
        return _do_collect(mailbox, message, pending, flow, ex, idx)
    return None, None, True


def _identifiers(mailbox, message, pending):
    """Every identifier we can see for this message: extracted from the body PLUS any already
    stored on the pending (folded in by _accumulate_pending on replies)."""
    from apps.ingestion import service

    o, p, e = service._tracking_identifiers(
        message, exclude_emails=[mailbox.email_address, pending.customer_email])
    return (o or pending.order_id or ""), (p or pending.phone or ""), e


def _do_verify(mailbox, message, pending, flow, ex, idx):
    """AUTO-VERIFY: try this message's identifiers against Shopify. Found+match (or escalation
    after MAX_VERIFY_ATTEMPTS) -> 'Verification successful.' + the next step's prompt. No
    identifier -> ask for one. Real NO-MATCH -> 'could not verify'."""
    from apps.ingestion import service

    vf = flow.get("verify_fail")               # (subject, body) override for this flow
    o, p, e = _identifiers(mailbox, message, pending)
    if not (o or p or e):
        logger.info("GUIDED-VERIFY pending=%s no_identifier -> ask", pending.id)
        if vf:
            _send(pending, vf[1], subject=vf[0])
        else:
            _send(pending, VERIFY_PROMPT)
        return None, None, True
    proceed, status, info = service._verify_against_shopify(mailbox.brand, o, p, e)
    attempts = (ex.get("verify_attempts") or 0) + 1
    ex["verify_attempts"] = attempts
    escalate = attempts >= service.MAX_VERIFY_ATTEMPTS
    logger.info("GUIDED-VERIFY pending=%s order=%s mobile=%s email=%s status=%s attempt=%s "
                "escalate=%s", pending.id, o or "-", p or "-", e or "-", status, attempts,
                escalate)
    if proceed or escalate:
        stamped = service._stamp_verified_customer({**ex}, info)
        stamped["verified"] = True
        if (info.get("customer_phone") or "").strip():
            stamped["verified_phone"] = info["customer_phone"].strip()
        if not proceed:
            stamped["verify_unconfirmed"] = status
        ex.clear()
        ex.update(stamped)
        nxt = idx + 1
        ex["step"] = nxt
        pending.extracted = ex
        pending.save(update_fields=["extracted", "updated_at"])
        steps = flow["steps"]
        if nxt < len(steps):
            subj = flow.get("collect_subject")
            prompt = steps[nxt]["prompt"]
            # Flows with a custom collect subject send their own wording; others prepend the
            # generic "Verification successful." acknowledgement.
            _send(pending, prompt if subj else (VERIFY_OK + "\n\n" + prompt), subject=subj)
            return None, None, True
        return _finish(mailbox, message, pending, flow, ex)
    pending.extracted = ex
    pending.save(update_fields=["extracted", "updated_at"])
    if vf:                                       # real no-match -> the flow's verify wording
        _send(pending, vf[1], subject=vf[0])
    else:
        _send(pending, VERIFY_FAIL)
    return None, None, True


def _do_collect(mailbox, message, pending, flow, ex, idx):
    """SINGLE-DETAIL collection: gather every required item from ONE reply; re-ask (once,
    consolidated) for only what is still missing -- never a chain of separate questions."""
    step = flow["steps"][idx]
    gd = ex.setdefault("guided_data", {})
    text = (message.get("body_text") or message.get("snippet") or "").strip()
    _gather_items(step["items"], gd, text)
    missing = [it for it in step["items"] if not _item_satisfied(it, gd, pending)]
    pending.extracted = ex
    pending.save(update_fields=["extracted", "updated_at"])
    if missing:
        logger.info("GUIDED-COLLECT pending=%s missing=%s", pending.id,
                    [m["key"] for m in missing])
        _send(pending, _collect_reask(missing))
        return None, None, True
    return _finish(mailbox, message, pending, flow, ex)


def _extract_mobile(text):
    """A VALID Indian mobile (bare 10 digits starting 6-9) -- rejects random numbers like
    1234567890. Used where the spec demands a real mobile (Update Address / Phone)."""
    from apps.classifier.rule_classifier import _extract_phone as _strict
    return _strict(text or "") or ""


def _gather_items(items, gd, text):
    for it in items:
        t = it["type"]
        if t == "email":
            em = _extract_email(text)
            if em:
                gd[it["key"]] = em
        elif t == "mobile":                     # strict: a valid mobile, not a random number
            mob = _extract_mobile(text)
            if mob:
                gd[it["key"]] = mob
        elif t == "phone":                      # lenient: any 10-digit contact number
            ph = _extract_phone(text)
            if ph:
                gd[it["key"]] = ph
        elif t in ("text", "issue"):
            if text and len(text) >= 3 and not gd.get(it["key"]):
                gd[it["key"]] = text
        # photo / video are satisfied from the pending's accumulated attachment flags.


def _item_satisfied(it, gd, pending):
    t = it["type"]
    if t == "photo":
        return pending.has_photo
    if t == "video":
        return pending.has_video
    return bool(gd.get(it["key"]))


def _collect_reask(missing):
    return ("Please also provide the following in your reply:\n\n"
            + "\n".join("• " + it["label"] for it in missing))


def _finish(mailbox, message, pending, flow, ex):
    from apps.ingestion import service

    # Keep the VERIFIED order-owner phone for the Care Panel (a reply carrying a NEW number --
    # e.g. Update Phone -- would otherwise have overwritten extracted['phone']).
    if ex.get("verified_phone"):
        ex["phone"] = ex["verified_phone"]
    pending.extracted = ex
    pending.save(update_fields=["extracted", "updated_at"])
    if flow["terminal"] == "auto_reply":
        _send(pending, flow["reply"], subject=flow.get("reply_subject"))
        service._close_inquiry(pending)
        pending.save(update_fields=["status", "closed_at", "updated_at"])
        logger.info("GUIDED-FLOW-COMPLETE flow=%s -> auto_reply (no ticket) pending=%s",
                    ex.get("guided_flow"), pending.id)
        return None, None, True
    # Ticket flow: a custom confirmation (Make Changes To Order) replaces the generic M5.
    tc = flow.get("ticket_confirmation")
    if tc:
        ex["guided_confirmation_subject"], ex["guided_confirmation_body"] = tc
        pending.extracted = ex
        pending.save(update_fields=["extracted", "updated_at"])
    logger.info("GUIDED-FLOW-COMPLETE flow=%s -> create ticket pending=%s data=%s",
                ex.get("guided_flow"), pending.id, list((ex.get("guided_data") or {}).keys()))
    ticket = service._promote_pending(mailbox, pending, message)
    return ticket, ticket.messages.order_by("created_at").last(), True
