"""
Category-first evidence policy (DeoDap Care — evidence workflow).

Given a classification (category + sub-topic + issue text), decide how much evidence
is required BEFORE a ticket can be created:

    EV_VIDEO  -> a video is mandatory (photo alone is not enough)
                 Defective Item / Missing Item / Wrong Item / Not Received
    EV_PHOTO  -> a photo is required, a video is optional
                 Damaged / Broken / Bad Quality
    EV_NONE   -> no photo/video needed
                 Tracking, Order Status, Refund, Return, General Inquiry, ...

The level is computed from (in order, taking the strongest):
  1. The matched SUB-TOPIC's DB flags (requires_video / requires_evidence) -- a floor
     that can only RAISE the requirement. (Category-level video is intentionally NOT
     used: it is too coarse -- it forced Damaged -> video just because it shares a
     category with Missing/Not-Received.)
  2. A deterministic keyword policy over the category + sub-topic + issue text, so the
     rule holds even when the AI returns a coarse category with no sub-topic mapped.
  3. The AI's own requires_evidence hint -> at least a photo, only if nothing stronger.

This keeps evidence rules CATEGORY-SPECIFIC and config-driven (no hardcoded ids in the
gating logic) while staying robust to coarse AI output.
"""

EV_NONE, EV_PHOTO, EV_VIDEO = "none", "photo", "video"
_RANK = {EV_NONE: 0, EV_PHOTO: 1, EV_VIDEO: 2}

# Video-mandatory intents: the item is wrong/defective/missing -> need a video to verify.
VIDEO_KEYWORDS = (
    "defective", "not working", "doesn't work", "does not work", "malfunction",
    "stopped working", "missing item", "missing product", "item missing",
    "items missing", "not received", "didn't receive", "did not receive",
    "wrong item", "wrong product", "incorrect item", "received wrong", "wrong size",
)
# Photo-evidence intents: damage/quality you can see in a still image. Also "wrong parcel"
# (a whole-parcel mix-up) -> photo evidence (POS + product images + shipping label), no video.
PHOTO_KEYWORDS = (
    "damaged", "damage", "broken", "bad quality", "poor quality", "quality issue",
    "cracked", "torn", "leaking", "scratch", "dented", "spoiled",
    "wrong parcel", "wrong package", "wrong shipment",
)

# Cancellation intents -- these take PRIORITY over damage/evidence keywords (a customer
# saying "cancel my damaged order" wants a CANCELLATION, not a damage-evidence workflow).
# Cancellation NEVER requires photo/video.
CANCEL_KEYWORDS = (
    "cancel order", "cancel the order", "cancel my order", "cancel this order",
    "order cancellation", "cancellation request", "want to cancel", "wish to cancel",
    "please cancel", "cancel and refund", "return and cancel",
)


def is_cancellation(text):
    """True if the text is an order-cancellation request (priority over damage)."""
    return any(k in (text or "").lower() for k in CANCEL_KEYWORDS)


# "Order Shown Delivered But Not Received": the courier marked the parcel DELIVERED but the
# customer never got it. This is a LOGISTICS dispute (Care Panel issue 3), NOT an item-
# condition case -- you cannot film an unboxing video of a package you never received, so
# it must require NO photo/video. It takes priority over the "not received" -> Missing-Item
# video keyword (which is for an item missing FROM a parcel that WAS received).
DELIVERED_STATUS_KEYWORDS = (
    "delivered", "marked delivered", "marked as delivered", "shows delivered",
    "showing delivered", "shown as delivered", "shown delivered", "status delivered",
    "tracking shows delivered", "status shows delivered", "showing as delivered",
)
NOT_RECEIVED_KEYWORDS = (
    "not received", "didn't receive", "did not receive", "haven't received",
    "have not received", "never received", "not got", "didn't get", "did not get",
    "nothing received", "no parcel", "no package", "not delivered to me",
)
# Physical item-condition descriptors mean the parcel WAS received (a damaged/defective/
# wrong/quantity/quality item). These OVERRIDE the non-delivery signal even when a
# 'not received' label is also present (AI mislabel) -- that is an evidence workflow,
# not the issue-3 non-delivery dispute.
ITEM_CONDITION_KEYWORDS = (
    "damaged", "damage", "broken", "crack", "cracked", "torn", "leak", "leaking",
    "leakage", "dented", "shattered", "defective", "not working", "doesn't work",
    "does not work", "malfunction", "dead product", "stopped working", "wrong item",
    "wrong product", "different product", "different item", "incorrect item",
    "received wrong", "quantity issue", "less quantity", "missing pieces",
    "bad quality", "poor quality", "low quality", "inferior quality", "quality issue",
)


def is_delivered_not_received(text):
    """True for the 'Order Shown Delivered But Not Received' dispute -- tracking says
    DELIVERED but the customer never got the package. No unboxing video is possible, so
    NO evidence is required (it's an agent/courier investigation, Care Panel issue 3).

    Defers to ITEM_CONDITION_KEYWORDS: if the customer describes a damaged/defective/wrong
    item, the parcel WAS received -> that is an evidence case, not a non-delivery dispute."""
    low = (text or "").lower()
    if any(k in low for k in ITEM_CONDITION_KEYWORDS):
        return False
    return (any(k in low for k in DELIVERED_STATUS_KEYWORDS)
            and any(k in low for k in NOT_RECEIVED_KEYWORDS))


# Deterministic Delivered-Item sub-type, by keyword. ORDER MATTERS: "Damaged" is checked
# FIRST so "my order is damage" can never fall through to "Missing Item". Each tuple is
# (sub-type name, keywords). Mirrors the business rules.
DELIVERED_ITEM_SUBTYPES = (
    ("Damaged Item", ("damaged", "damage", "broken", "crack", "cracked", "torn", "leak",
                      "leakage", "leaking", "physically damaged", "dented", "shattered")),
    ("Defective Item", ("defective", "not working", "doesn't work", "does not work",
                        "malfunction", "dead product", "stopped working")),
    ("Wrong Item", ("wrong item", "wrong product", "different product", "different item",
                    "incorrect item", "received wrong")),
    ("Quantity Issue", ("quantity issue", "less quantity", "missing pieces", "fewer pieces",
                        "short quantity", "less pieces", "fewer items")),
    ("Quality Issue", ("quality issue", "bad quality", "poor quality", "low quality",
                       "inferior quality")),
    ("Missing Item", ("missing", "item not received", "not received", "didn't receive",
                      "did not receive")),
)


def delivered_item_subtype(text):
    """Return the Delivered-Item sub-type name for `text` ('Damaged Item' / 'Defective Item'
    / 'Wrong Item' / 'Quantity Issue' / 'Missing Item'), or None if no keyword matches.
    Deterministic and order-sensitive -- 'damage' resolves to 'Damaged Item', NEVER
    'Missing Item'."""
    # "Delivered but not received" is a NON-DELIVERY dispute, not a delivered-item sub-type
    # (no parcel arrived to inspect) -- never mislabel it 'Missing Item'.
    if is_delivered_not_received(text):
        return None
    low = (text or "").lower()
    for subtype, keywords in DELIVERED_ITEM_SUBTYPES:
        if any(k in low for k in keywords):
            return subtype
    return None

# --------------------------------------------------------------------------------------- #
# Delivered-Item evidence REQUIREMENTS (customer-facing rules for the "Delivered Item
# Related" category). This is EVIDENCE-ONLY: it decides which stored files are mandatory
# before a ticket and which auto-reply requests them. It is deliberately SEPARATE from
# delivered_item_subtype() (which drives the Care Panel issue mapping) so category
# classification and the Care Panel integration are left untouched. need_photo / need_video
# are validated against the EXISTING has_photo / has_video detection.
# --------------------------------------------------------------------------------------- #
EV_CASE_DAMAGED = "damaged"
EV_CASE_NON_WORKING = "non_working"
EV_CASE_MISSING = "missing"
EV_CASE_WRONG_PRODUCT = "wrong_product"
EV_CASE_WRONG_PARCEL = "wrong_parcel"
EV_CASE_DEFECTIVE = "defective"

# ORDER MATTERS -- most specific first. Non-working is kept distinct from Defective, and Wrong
# Parcel (the whole parcel is someone else's) distinct from Wrong Product (one wrong item).
_DELIVERED_EVIDENCE_CASES = (
    (EV_CASE_WRONG_PARCEL, ("wrong parcel", "wrong package", "wrong shipment", "entire parcel",
                            "whole parcel", "different parcel", "not my order", "not my parcel",
                            "someone else", "another person")),
    (EV_CASE_NON_WORKING, ("not working", "doesn't work", "does not work", "won't work",
                           "stopped working", "won't turn on", "not turning on", "won't switch on",
                           "not switching on", "won't power on", "not powering on",
                           "dead on arrival", "won't charge", "not charging")),
    (EV_CASE_DEFECTIVE, ("defective", "defect", "faulty", "malfunction", "malfunctioning")),
    (EV_CASE_DAMAGED, ("damaged", "damage", "broken", "crack", "cracked", "torn", "leak",
                       "leakage", "leaking", "dented", "shattered", "scratched")),
    (EV_CASE_WRONG_PRODUCT, ("wrong item", "wrong product", "different product", "different item",
                             "incorrect item", "incorrect product", "received wrong",
                             "wrong article")),
    (EV_CASE_MISSING, ("missing", "item not received", "product not received", "not received",
                       "didn't receive", "did not receive")),
)

# Per case: mandatory FILE evidence (checked via has_photo / has_video) + the mail template id.
# SKU / product count are requested in the mail text but are free-text the customer types in the
# same reply -- they are not separately file-gated (there is no reliable attachment for them).
DELIVERED_EVIDENCE_RULES = {
    EV_CASE_DAMAGED:       {"photo": True,  "video": True,  "mail": "EV_DAMAGED"},
    EV_CASE_NON_WORKING:   {"photo": False, "video": True,  "mail": "EV_NON_WORKING"},
    EV_CASE_MISSING:       {"photo": True,  "video": True,  "mail": "EV_MISSING"},
    EV_CASE_WRONG_PRODUCT: {"photo": True,  "video": True,  "mail": "EV_WRONG_PRODUCT"},
    EV_CASE_WRONG_PARCEL:  {"photo": True,  "video": False, "mail": "EV_WRONG_PARCEL"},
    EV_CASE_DEFECTIVE:     {"photo": True,  "video": True,  "mail": "EV_DEFECTIVE"},
}


def delivered_evidence_case(text):
    """Return the Delivered-Item evidence CASE (one of EV_CASE_*) for `text`, or None.

    Evidence-only: it selects the exact evidence-request wording + the mandatory files. Deferred
    to is_delivered_not_received (a whole parcel never arrived -> no unboxing evidence possible).
    Does NOT change classification or the Care Panel issue mapping."""
    if is_delivered_not_received(text):
        return None
    low = (text or "").lower()
    for case, keywords in _DELIVERED_EVIDENCE_CASES:
        if any(k in low for k in keywords):
            return case
    return None


# File-type detection for evidence already attached. Used to scan stored attachments /
# message parts so we NEVER re-ask for evidence the customer already sent.
PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic")
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp")


def is_photo(filename="", content_type=""):
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return True
    return (filename or "").lower().endswith(PHOTO_EXTS)


def is_video(filename="", content_type=""):
    ct = (content_type or "").lower()
    if ct.startswith("video/"):
        return True
    return (filename or "").lower().endswith(VIDEO_EXTS)


def scan_attachments(items):
    """Scan (filename, content_type) pairs -> (has_photo, has_video) by MIME + extension."""
    has_photo = has_video = False
    for filename, content_type in items:
        if is_video(filename, content_type):
            has_video = True
        elif is_photo(filename, content_type):
            has_photo = True
    return has_photo, has_video


def policy_for_text(text):
    """Keyword policy: EV_VIDEO / EV_PHOTO / EV_NONE from free text."""
    t = (text or "").lower()
    if any(k in t for k in VIDEO_KEYWORDS):
        return EV_VIDEO
    if any(k in t for k in PHOTO_KEYWORDS):
        return EV_PHOTO
    return EV_NONE


def _bump(cur, new):
    return new if _RANK[new] > _RANK[cur] else cur


def evidence_level(*, category="", sub_topic="", issue_summary="", text="",
                   category_ref=None, sub_topic_ref=None, ai_requires_evidence=False):
    """Return EV_NONE | EV_PHOTO | EV_VIDEO -- the evidence required before a ticket."""
    # 0) CANCELLATION has the HIGHEST priority and NEVER requires evidence -- a customer
    #    asking to cancel must not be dropped into the damage-evidence workflow, even if
    #    the AI mis-mapped it or the text also mentions damage.
    combined = " ".join(filter(None, [text, issue_summary, sub_topic, category]))
    if is_cancellation(combined):
        return EV_NONE

    # 0b) "Order Shown Delivered But Not Received" -- the whole parcel never arrived despite a
    #     DELIVERED status. An unboxing video is impossible -> NEVER require evidence. This
    #     beats the "not received" -> Missing-Item video keyword (item missing FROM a parcel).
    if is_delivered_not_received(combined):
        return EV_NONE

    # 0c) Payment deducted but order NOT placed -> a PAYMENT SCREENSHOT (photo). NEVER a video
    #     (an unboxing video makes no sense) and never the Missing-Item "not received" keyword.
    from apps.decision import policy
    if policy.payment_no_order(combined) or "payment deducted but order not placed" in (
            (sub_topic or "") + " " + (getattr(sub_topic_ref, "name", "") if sub_topic_ref else "")
            ).lower():
        return EV_PHOTO

    level = EV_NONE

    # 1) Sub-topic DB flags (admin config = a floor that can only raise the requirement).
    #    Category-level video is deliberately ignored -- too coarse (it forced Damaged
    #    to video just for sharing a category with Missing / Not-Received).
    if sub_topic_ref is not None:
        if getattr(sub_topic_ref, "requires_video", False):
            level = _bump(level, EV_VIDEO)
        elif getattr(sub_topic_ref, "requires_evidence", False):
            level = _bump(level, EV_PHOTO)

    # 2) Category-first keyword policy over category + sub-topic + issue text.
    names = " ".join(filter(None, [
        category or (getattr(category_ref, "name", "") if category_ref else ""),
        sub_topic or (getattr(sub_topic_ref, "name", "") if sub_topic_ref else ""),
        issue_summary,
    ]))
    level = _bump(level, policy_for_text(names))

    # 3) AI hint: at least a photo, only when nothing stronger fired.
    if level == EV_NONE and ai_requires_evidence:
        level = EV_PHOTO

    return level


def requires_evidence(level):
    return _RANK[level] >= _RANK[EV_PHOTO]


def requires_video(level):
    return level == EV_VIDEO
