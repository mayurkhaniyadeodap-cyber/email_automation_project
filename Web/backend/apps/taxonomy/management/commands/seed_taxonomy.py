"""
Seed the fixed 16-category taxonomy for a brand (doc section 4).

Creates all 16 categories plus the sub-topics + IF/THEN/Action rules + templates
that the roadmap explicitly documents (sections 5-7 & 12). The remaining sub-topics
of the 83 are added per-brand through Settings / admin -- nothing is hardcoded.

Usage:
    python manage.py seed_taxonomy --brand <brand_id>
    python manage.py seed_taxonomy --all
"""

from django.core.management.base import BaseCommand, CommandError

from apps.organizations.models import Brand
from apps.taxonomy.models import Category, Rule, SubTopic, Template

# The 16 fixed categories.
CATEGORIES = [
    (1, "Shipment & Delivery Tracking"),
    (2, "Delivery Address & Customer Info Changes"),
    (3, "Delivery Issues (Post-Delivery)"),
    (4, "RTO (Return to Origin)"),
    (5, "Order Placement & Modification"),
    (6, "Order Cancellation"),
    (7, "Return, Refund & Replacement"),
    (8, "Payment & Invoice"),
    (9, "Product Information & Inquiry"),
    (10, "Offers, Discounts & Loyalty"),
    (11, "Wholesale / Bulk Purchase (B2B)"),
    (12, "Delivery Coverage & Shipping"),
    (13, "Store & Company Information"),
    (14, "Account Management & Security"),
    (15, "App & Website Technical Issues"),
    (16, "Feedback, Support & Fraud"),
]

# Documented sub-topics: code -> dict(name, question, mandatory, sensitive, rules, template)
# rules: list of (condition, then_response, action)
A = Rule  # short alias for action constants
SUBTOPICS = {
    1: [
        {
            "code": "1.1",
            "name": "Shipment Status",
            "question": "Where is my order / when will it come?",
            "mandatory": ["order_id"],
            # Always auto-answer with a tracking link. {tracking_url} is the live link
            # when Shopify/Ship are configured, else our internal /t page. No {edd}
            # placeholder -- we never promise a delivery date we can't substantiate.
            "rules": [
                ("Always",
                 "Hi! We've received your request for order {order_id}. "
                 "You can track the status here: {tracking_url}.",
                 A.ACTION_INFO_ONLY),
            ],
            "template": ("Hi! We've received your request for order {order_id}. "
                         "You can track the status here: {tracking_url}."),
        },
        {
            "code": "1.3",
            "name": "Delay",
            "question": "My order is late / past the expected delivery date.",
            "mandatory": ["order_id"],
            "rules": [
                ("Shipped AND EDD breached",
                 "Apology + current status; escalate to agent.",
                 A.ACTION_CREATE_TICKET),
            ],
        },
    ],
    3: [
        {
            "code": "3.1",
            "name": "Marked Delivered but Not Received",
            "question": "Tracking says delivered but I didn't receive it.",
            "mandatory": ["order_id"],
            "evidence": True,
            "video": True,
            "rules": [
                ("Marked delivered but customer reports not received",
                 "Open POD investigation with courier; HIGH priority if > 48h.",
                 A.ACTION_CREATE_TICKET),
            ],
        },
        {
            "code": "3.3",
            "name": "Damaged Item",
            "question": "My product arrived broken / damaged.",
            "mandatory": ["order_id"],
            # Damaged -> a PHOTO is enough (a video is optional). Not video-mandatory.
            "evidence": True,
            "video": False,
            "rules": [
                ("No unboxing video / photo evidence present",
                 "Please share a clear photo of the damaged item.",
                 A.ACTION_AWAIT_EVIDENCE),
                ("Evidence present",
                 "Complaint registered; routed to agent for resolution.",
                 A.ACTION_CREATE_TICKET),
            ],
            "template": ("Sorry to hear that! To process your claim quickly, please "
                         "reply with an unboxing video or a clear photo of the damage."),
        },
    ],
    6: [
        {
            "code": "6.1",
            "name": "Cancel / Refund (pre-dispatch)",
            "question": "Cancel my order, it hasn't shipped yet.",
            "mandatory": ["order_id"],
            "rules": [
                ("Not dispatched AND not a custom item",
                 "Trigger cancellation + refund.",
                 A.ACTION_TRIGGER_CRP),
            ],
        },
    ],
    8: [
        {
            "code": "8.4",
            "name": "Extra / Double Payment Refund",
            "question": "I was charged twice / paid extra.",
            "mandatory": ["order_id"],
            "sensitive": True,
            "rules": [
                ("Any double/extra payment refund",
                 "Always human review (money-touching).",
                 A.ACTION_CREATE_TICKET),
            ],
        },
    ],
    12: [
        {
            "code": "12.1",
            "name": "Pincode / Serviceability",
            "question": "Do you deliver to my pincode / area?",
            "mandatory": [],
            "rules": [
                ("Always",
                 "Share serviceability info for the pincode.",
                 A.ACTION_INFO_ONLY),
            ],
            "template": ("Yes/No — we currently {serviceability} deliver to {pincode}. "
                         "Standard delivery is {edd_days} days."),
        },
    ],
    14: [
        {
            "code": "14.3",
            "name": "Account Deletion",
            "question": "Please delete my account / data.",
            "mandatory": [],
            "sensitive": True,
            "rules": [
                ("Any account deletion request",
                 "Always human (sensitive / compliance).",
                 A.ACTION_CREATE_TICKET),
            ],
        },
    ],
    15: [
        {"code": "15.1", "name": "App Crashing / Not Loading",
         "question": "The app keeps crashing / will not load.",
         "evidence": True, "video": True,
         "rules": [("App or website fault", "Collect issue + screenshot + video; create ticket.",
                    A.ACTION_CREATE_TICKET)]},
        {"code": "15.2", "name": "Cart Not Saving Items",
         "question": "Items do not stay saved in my cart.",
         "evidence": True, "video": True,
         "rules": [("Cart fault", "Collect issue + screenshot + video; create ticket.",
                    A.ACTION_CREATE_TICKET)]},
        {"code": "15.3", "name": "Checkout Page Not Load",
         "question": "The checkout page will not load.",
         "evidence": True, "video": True,
         "rules": [("Checkout fault", "Collect issue + screenshot + video; create ticket.",
                    A.ACTION_CREATE_TICKET)]},
        {"code": "15.4", "name": "Update Phone / Email",
         "question": "I need to update my registered phone / email.",
         "rules": [("Contact update", "Collect new phone / email; create ticket.",
                    A.ACTION_CREATE_TICKET)]},
        {"code": "15.5", "name": "OTP / Notifications Not Received",
         "question": "I am not receiving OTP / notifications.",
         "rules": [("OTP / notifications", "Collect email + mobile; create ticket.",
                    A.ACTION_CREATE_TICKET)]},
    ],
    16: [
        {
            "code": "16.2",
            "name": "Report Fraud",
            "question": "I got a scam call / fraud asking for OTP / payment.",
            "mandatory": [],
            "sensitive": True,
            "rules": [
                ("Any fraud report",
                 "Human only, HIGH priority ticket.",
                 A.ACTION_CREATE_TICKET),
            ],
        },
    ],
}


class Command(BaseCommand):
    help = "Seed the fixed 16-category taxonomy (+ documented sub-topics) for a brand."

    def add_arguments(self, parser):
        parser.add_argument("--brand", type=int, help="Brand id to seed.")
        parser.add_argument("--all", action="store_true", help="Seed every brand.")

    def handle(self, *args, **opts):
        if opts["all"]:
            brands = list(Brand.objects.all())
        elif opts["brand"]:
            brands = list(Brand.objects.filter(pk=opts["brand"]))
            if not brands:
                raise CommandError(f"No brand with id {opts['brand']}")
        else:
            raise CommandError("Pass --brand <id> or --all")

        for brand in brands:
            self._seed_brand(brand)

    def _seed_brand(self, brand):
        cats_created = subs_created = subs_updated = 0
        for code, name in CATEGORIES:
            cat, made = Category.objects.get_or_create(
                brand=brand, code=str(code),
                defaults={"name": name, "position": code},
                # NOTE: evidence is CATEGORY-FIRST but SUB-TOPIC specific now -- the
                # video/photo requirement is decided per sub-topic + the keyword policy
                # (apps.ingestion.evidence), not by a blunt category-wide video flag
                # (which wrongly forced Damaged -> video). See SUBTOPICS flags below.
            )
            cats_created += int(made)

            for st in SUBTOPICS.get(code, []):
                sub, smade = SubTopic.objects.get_or_create(
                    category=cat, code=st["code"],
                    defaults={
                        "name": st["name"],
                        "question": st.get("question", ""),
                        "mandatory_inputs": st.get("mandatory", []),
                        "requires_evidence": st.get("evidence", False),
                        "requires_video": st.get("video", False),
                        "is_sensitive": st.get("sensitive", False),
                        "position": int(st["code"].split(".")[1]),
                    },
                )
                subs_created += int(smade)
                if smade:
                    for i, (cond, then, act) in enumerate(st.get("rules", []), 1):
                        Rule.objects.create(
                            sub_topic=sub, condition=cond,
                            then_response=then, action=act, position=i,
                        )
                    if st.get("template"):
                        Template.objects.create(
                            sub_topic=sub, name="default", body=st["template"]
                        )
                else:
                    # Re-seeding is authoritative for the EVIDENCE flags of the documented
                    # sub-topics, so a stale flag (e.g. Damaged 3.3 left as video) is
                    # corrected in place. Other admin-edited fields are left untouched.
                    sub.requires_evidence = st.get("evidence", False)
                    sub.requires_video = st.get("video", False)
                    if not sub.mandatory_inputs:
                        sub.mandatory_inputs = st.get("mandatory", [])
                    sub.save(update_fields=["requires_evidence", "requires_video",
                                            "mandatory_inputs", "updated_at"])
                    subs_updated += 1

        # Category-level video flag is no longer used by the evidence policy; clear any
        # stale ones from the old seed so they can't act as a hidden video floor.
        stale = Category.objects.filter(brand=brand, requires_video=True).update(
            requires_video=False)

        self.stdout.write(self.style.SUCCESS(
            f"[{brand}] categories +{cats_created}, sub-topics +{subs_created}, "
            f"sub-topics updated {subs_updated}, stale category video flags cleared {stale}."
        ))
