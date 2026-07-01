"""
Shipment Tracking returns the ACTUAL order status (lookup order -> fulfillment -> AWB ->
courier), never the generic 'looking into it' acknowledgement, and never a ticket.

    python manage.py test apps.ingestion.tests_tracking_status
"""

from django.test import TestCase, override_settings

from apps.classifier.service import ClassificationResult
from apps.ingestion import service
from apps.ingestion.tests_imap import FakeImap
from apps.ingestion.tests_smart import eml
from apps.integrations.context import lookup_tracking
from apps.organizations.models import Brand, Mailbox, Organization
from apps.taxonomy.models import Category, Rule, SubTopic
from apps.tickets.models import Message, PendingConversation, Ticket


class FakeShopify:
    def __init__(self, orders=None, by_phone=None, by_email=None):
        self.orders = orders or {}
        self.by_phone = by_phone or {}
        self.by_email = by_email or {}
        self.calls = []                       # records every lookup made

    def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        return self.orders.get(order_id)

    def recent_orders_by_phone(self, phone, limit=5):
        self.calls.append(("by_phone", phone))
        return self.by_phone.get(phone, [])

    def recent_orders_by_email(self, email, limit=5):
        self.calls.append(("by_email", email))
        return self.by_email.get(email, [])


class FakeShipping:
    def __init__(self, by_awb):
        self.by_awb = by_awb

    def track(self, awb):
        return self.by_awb.get(awb)


ORDER = {"shipped": True, "delivered": False, "edd": "18 Jun 2026",
         "tracking_url": "https://track/AWB123", "awb": "AWB123", "courier": "",
         "raw_fulfillment_status": "In Transit"}     # RAW status the API returned
TRACK = {"status": "in_transit", "raw_status": "In Transit", "shipped": True,
         "delivered": False, "edd": "18 Jun 2026", "tracking_url": "https://track/AWB123",
         "courier": "Delhivery", "awb": "AWB123"}


class LookupTrackingTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")

    def _clients(self, **kw):
        return {"shopify": kw.get("shopify"), "shipping": kw.get("shipping"), "gokwik": None}

    def test_found_with_live_courier_tracking(self):
        clients = self._clients(shopify=FakeShopify({"486324": dict(ORDER)}),
                                shipping=FakeShipping({"AWB123": dict(TRACK)}))
        info = lookup_tracking(self.brand, "486324", clients=clients)
        self.assertTrue(info["found"])
        self.assertEqual(info["status"], "in_transit")
        self.assertEqual(info["courier"], "Delhivery")
        self.assertEqual(info["awb"], "AWB123")
        self.assertEqual(info["edd"], "18 Jun 2026")

    def test_found_without_shipping_client_derives_status(self):
        clients = self._clients(shopify=FakeShopify({"486324": dict(ORDER)}), shipping=None)
        info = lookup_tracking(self.brand, "486324", clients=clients)
        self.assertTrue(info["found"])
        self.assertEqual(info["status"], "in_transit")     # shipped -> in transit
        self.assertEqual(info["awb"], "AWB123")

    def test_not_found_is_distinct_from_unavailable(self):
        clients = self._clients(shopify=FakeShopify({}), shipping=None)   # configured, empty
        info = lookup_tracking(self.brand, "999", clients=clients)
        self.assertTrue(info["configured"])
        self.assertFalse(info["found"])

    def test_not_configured(self):
        info = lookup_tracking(self.brand, "486324", clients=self._clients())
        self.assertFalse(info["configured"])
        self.assertFalse(info["found"])

    def test_seller_order_falls_back_to_care_panel(self):
        # The reported bug: order 577481 is a seller/marketplace order NOT in Shopify -> instead
        # of a generic 'not found', resolve real tracking from the Care Panel shipment API.
        from apps.integrations import care_panel
        orig = care_panel.fetch_shipment_flow
        care_panel.fetch_shipment_flow = lambda brand, oid, **k: {
            "shipment_status": "Delivered", "order_status": "Delivered",
            "tracking_url": "https://ship.deodap.in/t/7X116200902",
            "awb": "7X116200902", "courier": "DTDC B2C", "edd": ""}
        try:
            info = lookup_tracking(self.brand, "577481",
                                   clients=self._clients(shopify=FakeShopify({}), shipping=None))
        finally:
            care_panel.fetch_shipment_flow = orig
        self.assertTrue(info["found"])
        self.assertEqual(info["matched_by"], "care_panel")
        self.assertEqual(info["awb"], "7X116200902")
        self.assertEqual(info["raw_status"], "Delivered")
        self.assertTrue(info["delivered"])
        self.assertEqual(info["tracking_url"], "https://ship.deodap.in/t/7X116200902")

    def test_no_match_anywhere_still_not_found(self):
        # Shopify empty AND Care Panel returns nothing -> genuinely not found (no false data).
        from apps.integrations import care_panel
        orig = care_panel.fetch_shipment_flow
        care_panel.fetch_shipment_flow = lambda brand, oid, **k: None
        try:
            info = lookup_tracking(self.brand, "999",
                                   clients=self._clients(shopify=FakeShopify({}), shipping=None))
        finally:
            care_panel.fetch_shipment_flow = orig
        self.assertFalse(info["found"])

    # === Track Live link in the status email (reported bug) ============================
    def test_tracking_email_contains_tracking_link(self):
        # A found order with a tracking URL renders a "Track Live" link in the email body.
        clients = self._clients(shopify=FakeShopify({"486324": dict(ORDER)}), shipping=None)
        info = lookup_tracking(self.brand, "486324", clients=clients)
        body = service._format_tracking_details(info)
        self.assertIn("Track Order:", body)
        self.assertIn(info["tracking_url"], body)

    # === Refund Status line (below AWB) ================================================
    def test_refund_status_line_in_email(self):
        from apps.integrations.context import compute_refund_status
        # All 5 states render directly below AWB.
        cases = [("refunded", "Delivered", None, "Refunded"),
                 ("partially_refunded", "Delivered", None, "Partially Refunded"),
                 ("paid", "RTO-Delivered", None,
                  "Pending verification after returned shipment reaches the warehouse."),
                 ("pending", "Cancelled", "2026-01-01", "Not Refunded"),
                 ("paid", "Delivered", None, "Not Applicable")]
        for fin, raw, cancelled, expected in cases:
            with self.subTest(fin=fin, raw=raw):
                self.assertEqual(compute_refund_status(fin, raw, cancelled), expected)
                info = {"order_id": "262324646", "raw_status": raw, "courier": "DTDC",
                        "awb": "7X116200942", "refund_status": expected,
                        "tracking_url": "https://t/x"}
                body = service._format_tracking_details(info)
                self.assertIn(f"AWB: 7X116200942\nRefund Status: {expected}", body)

    def test_lookup_sets_refund_status_refunded(self):
        order = {"shipped": True, "delivered": True, "financial_status": "refunded",
                 "raw_fulfillment_status": "RTO-Delivered", "awb": "AWB1", "tracking_url": "u"}
        info = lookup_tracking(self.brand, "486324",
                               clients=self._clients(shopify=FakeShopify({"486324": order})))
        self.assertEqual(info["refund_status"], "Refunded")
        self.assertIn("Refund Status: Refunded", service._format_tracking_details(info))

    def test_awb_generates_ship_deodap_link(self):
        # Shopify returns an AWB but NO tracking_url and there's no courier client ->
        # build https://ship.deodap.in/tracking/<awb> so the link is never missing.
        order = {"shipped": True, "awb": "7D130624612", "tracking_url": "", "courier": ""}
        clients = self._clients(shopify=FakeShopify({"486324": order}), shipping=None)
        info = lookup_tracking(self.brand, "486324", clients=clients)
        self.assertEqual(info["tracking_url"], "https://ship.deodap.in/tracking/7D130624612")
        self.assertIn("Track Order:\nhttps://ship.deodap.in/tracking/7D130624612",
                      service._format_tracking_details(info))

    def test_shopify_tracking_url_used_when_available(self):
        # A real Shopify/courier URL WINS -- we never overwrite it with the ship.deodap link.
        order = {"shipped": True, "awb": "AWB123", "tracking_url": "https://track/AWB123"}
        clients = self._clients(shopify=FakeShopify({"486324": order}), shipping=None)
        info = lookup_tracking(self.brand, "486324", clients=clients)
        self.assertEqual(info["tracking_url"], "https://track/AWB123")

    def test_no_link_only_when_courier_orderstatus_and_awb_all_missing(self):
        # A link is omitted ONLY when all three sources are absent.
        order = {"shipped": True, "awb": "", "tracking_url": "", "courier": "",
                 "order_status_url": ""}
        clients = self._clients(shopify=FakeShopify({"486324": order}), shipping=None)
        info = lookup_tracking(self.brand, "486324", clients=clients)
        self.assertEqual(info["tracking_url"], "")
        self.assertEqual(info["tracking_link_source"], "none")
        self.assertNotIn("Track Order:", service._format_tracking_details(info))

    def test_tracking_link_rendered_as_hyperlink_not_raw_url(self):
        # BUG 2: the customer never sees the full URL -- it is a 'View Order Status' link.
        url = "https://deodap3.myshopify.com/orders/abc?key=verylongtoken123"
        html = service._email_html("Track Order:\n" + url, url)
        self.assertIn('<a href="https://deodap3.myshopify.com/orders/abc?key=verylongtoken123">'
                      'View Order Status</a>', html)
        # the URL appears ONLY inside href -- never as visible link text.
        self.assertNotIn(">" + url, html)
        self.assertNotIn(url + "<", html)

    # === Track-Order link priority: courier -> order_status_url -> AWB ==================
    def test_link_priority_courier_url_wins(self):
        order = {"shipped": True, "awb": "AWB123", "tracking_url": "https://track/AWB123",
                 "order_status_url": "https://shop.myshopify.com/orders/abc"}
        info = lookup_tracking(self.brand, "486324",
                               clients=self._clients(shopify=FakeShopify({"486324": order})))
        self.assertEqual(info["tracking_url"], "https://track/AWB123")
        self.assertEqual(info["tracking_link_source"], "courier")

    def test_link_order_status_url_used_when_no_courier_url(self):
        # No courier URL but Shopify gave an order_status_url -> it WINS over the AWB build.
        order = {"shipped": True, "awb": "AWB123", "tracking_url": "",
                 "order_status_url": "https://shop.myshopify.com/orders/abc"}
        info = lookup_tracking(self.brand, "486324",
                               clients=self._clients(shopify=FakeShopify({"486324": order})))
        self.assertEqual(info["tracking_url"], "https://shop.myshopify.com/orders/abc")
        self.assertEqual(info["tracking_link_source"], "shopify_order_status_url")
        self.assertIn("Track Order:\nhttps://shop.myshopify.com/orders/abc",
                      service._format_tracking_details(info))

    def test_link_order_status_url_restores_link_when_no_awb(self):
        # The regression: cancelled/unfulfilled order, no AWB, no courier URL, but Shopify
        # always provides order_status_url -> the link is RESTORED (previously missing).
        order = {"shipped": False, "awb": "", "tracking_url": "", "raw_fulfillment_status": "",
                 "cancelled_at": "2026-06-18T10:00:00Z",
                 "order_status_url": "https://shop.myshopify.com/orders/xyz"}
        info = lookup_tracking(self.brand, "262098591",
                               clients=self._clients(shopify=FakeShopify({"262098591": order})))
        self.assertEqual(info["raw_status"], "Cancelled")
        self.assertEqual(info["tracking_url"], "https://shop.myshopify.com/orders/xyz")
        self.assertEqual(info["tracking_link_source"], "shopify_order_status_url")
        body = service._format_tracking_details(info)
        self.assertIn("Status: Cancelled", body)
        self.assertIn("Track Order:\nhttps://shop.myshopify.com/orders/xyz", body)

    def test_link_awb_built_when_no_courier_or_order_status(self):
        order = {"shipped": True, "awb": "7D130624612", "tracking_url": "",
                 "order_status_url": ""}
        info = lookup_tracking(self.brand, "486324",
                               clients=self._clients(shopify=FakeShopify({"486324": order})))
        self.assertEqual(info["tracking_url"], "https://ship.deodap.in/tracking/7D130624612")
        self.assertEqual(info["tracking_link_source"], "awb")

    # === RAW STATUS mode: the EXACT API status is shown, never mapped/grouped ==========
    def _status_email(self, *, courier_status=None, fulfillment=None, order_status=None):
        """Run a lookup where the status comes from courier / Shopify and return the
        rendered email status block + the resolved info."""
        order = {"shipped": True, "awb": "AWB9", "tracking_url": "https://track/AWB9"}
        if fulfillment is not None:
            order["raw_fulfillment_status"] = fulfillment
        if order_status is not None:
            order["raw_order_status"] = order_status
        shipping = None
        if courier_status is not None:
            shipping = FakeShipping({"AWB9": {"status": courier_status.lower().replace(" ", "_"),
                                              "raw_status": courier_status, "awb": "AWB9"}})
        info = lookup_tracking(self.brand, "486324",
                               clients=self._clients(shopify=FakeShopify({"486324": order}),
                                                     shipping=shipping))
        return service._format_tracking_details(info), info

    def test_raw_courier_statuses_shown_verbatim(self):
        # Non-RTO courier statuses appear EXACTLY (no Cancelled->In Transit, no NDR->Ndr).
        for raw in ["Cancelled", "In Transit", "Out For Delivery", "Delivered",
                    "Manifested", "Pending Pickup", "Processing", "NDR"]:
            body, info = self._status_email(courier_status=raw)
            self.assertEqual(info["status_source"], "courier")
            self.assertIn(f"Status: {raw}", body, f"{raw!r} was altered")

    def test_rto_courier_statuses_labelled_rto(self):
        # Every RTO variant is presented as 'Return To Origin (RTO)' with the return note --
        # and the courier status is used (never Shopify 'fulfilled').
        for raw in ["RTO", "RTO In Transit", "RTO Delivered", "Return To Origin"]:
            body, info = self._status_email(courier_status=raw, fulfillment="fulfilled")
            self.assertEqual(info["status_source"], "courier", raw)
            self.assertIn("Status: Return To Origin (RTO)", body, f"{raw!r} not mapped")
            self.assertIn("Your shipment is currently being returned to the seller.", body)
            self.assertNotIn("fulfilled", body.lower())

    def test_courier_status_used_over_shopify_fulfilled(self):
        # The reported bug: Shopify says 'fulfilled' but the courier says Return To Origin ->
        # the email must show RTO (never 'fulfilled'), the return note, and the RTO refund status.
        order = {"shipped": True, "awb": "AWB9", "tracking_url": "https://track/AWB9",
                 "raw_fulfillment_status": "fulfilled", "financial_status": "paid"}
        ship = FakeShipping({"AWB9": {"raw_status": "Return To Origin", "awb": "AWB9"}})
        info = lookup_tracking(self.brand, "486324", clients=self._clients(
            shopify=FakeShopify({"486324": order}), shipping=ship))
        self.assertEqual(info["status_source"], "courier")            # courier beats fulfillment
        body = service._format_tracking_details(info)
        self.assertIn("Status: Return To Origin (RTO)", body)
        self.assertNotIn("fulfilled", body.lower())
        self.assertIn("Your shipment is currently being returned to the seller.", body)
        self.assertIn("Refund Status: Pending verification after returned shipment reaches "
                      "the warehouse.", body)

    def test_status_mapping_table(self):
        # The required mapping: identity for all except Return To Origin -> Return To Origin (RTO).
        for raw, shown in [("Delivered", "Delivered"), ("In Transit", "In Transit"),
                           ("Out For Delivery", "Out For Delivery"),
                           ("Return To Origin", "Return To Origin (RTO)"),
                           ("Returned", "Returned"), ("Cancelled", "Cancelled")]:
            with self.subTest(raw=raw):
                body = service._format_tracking_details(
                    {"order_id": "1", "raw_status": raw, "refund_status": "Not Applicable"})
                self.assertIn(f"Status: {shown}", body)

    def test_portal_status_fallback_when_apis_down(self):
        # Reported bug: the shipment-flow API + AWB courier track are down (404), but the public
        # ship.deodap.in portal renders the LIVE status -> read it from there, never fall back to
        # Shopify 'fulfilled'.
        from unittest import mock
        from apps.integrations import context as ctx
        order = {"shipped": True, "awb": "7D130639143", "tracking_url": "",
                 "raw_fulfillment_status": "fulfilled", "financial_status": "paid",
                 "order_status_url": "https://shop/orders/x"}
        ship = FakeShipping({})                               # courier track returns None (down)
        with self._with_care_panel(None), \
                mock.patch.object(ctx, "scrape_portal_status", return_value="Return To Origin"):
            info = lookup_tracking(self.brand, "262182531", clients=self._clients(
                shopify=FakeShopify({"262182531": order}), shipping=ship))
        self.assertEqual(info["status_source"], "courier")   # portal status enters as courier
        self.assertEqual(info["raw_status"], "Return To Origin")
        body = service._format_tracking_details(info)
        self.assertIn("Status: Return To Origin (RTO)", body)
        self.assertNotIn("fulfilled", body.lower())

    def test_raw_shopify_fulfillment_status_shown_when_no_courier(self):
        body, info = self._status_email(fulfillment="Unfulfilled")
        self.assertEqual(info["status_source"], "shopify_fulfillment")
        self.assertIn("Status: Unfulfilled", body)

    def test_raw_shopify_order_status_shown_when_no_courier_or_fulfillment(self):
        body, info = self._status_email(order_status="Pending Pickup")
        self.assertEqual(info["status_source"], "shopify_order")
        self.assertIn("Status: Pending Pickup", body)

    def test_refunded_financial_status_beats_courier_and_fulfillment(self):
        # New priority: Shopify financial_status (refund/void) ranks above courier/fulfillment.
        body, info = self._status_email(courier_status="Out For Delivery",
                                        fulfillment="Unfulfilled", order_status="Refunded")
        self.assertEqual(info["status_source"], "shopify_financial")
        self.assertIn("Status: Refunded", body)

    def test_courier_status_wins_over_fulfillment(self):
        # Courier status overrides fulfillment when financial is a normal (non-terminal) state.
        body, info = self._status_email(courier_status="Out For Delivery",
                                        fulfillment="Unfulfilled", order_status="paid")
        self.assertEqual(info["status_source"], "courier")
        self.assertIn("Status: Out For Delivery", body)

    def test_cancelled_order_shows_cancelled_not_fulfilled(self):
        # The reported bug: a CANCELLED order whose fulfillment_status is still "fulfilled"
        # must read "Cancelled" -- cancellation is the HIGHEST priority.
        order = {"shipped": True, "awb": "AWB9", "tracking_url": "https://track/AWB9",
                 "raw_fulfillment_status": "fulfilled", "cancelled_at": "2026-06-18T10:00:00Z",
                 "cancel_reason": "customer"}
        info = lookup_tracking(self.brand, "486324",
                               clients=self._clients(shopify=FakeShopify({"486324": order})))
        self.assertEqual(info["status_source"], "shopify_cancelled")
        self.assertEqual(info["raw_status"], "Cancelled")
        self.assertIn("Status: Cancelled", service._format_tracking_details(info))

    def _with_care_panel(self, cp_return):
        """Patch care_panel.fetch_shipment_flow to return a fixed shipment-flow dict."""
        from apps.integrations import care_panel
        from unittest import mock
        return mock.patch.object(care_panel, "fetch_shipment_flow",
                                 lambda *a, **k: cp_return)

    def test_care_panel_shipment_status_wins_over_fulfilled(self):
        # The reported bug: Shopify says fulfilled (cancelled_at null), but the Care Panel
        # shipment API marks the order Cancelled -> the customer must see "Cancelled".
        order = {"shipped": True, "awb": "", "raw_fulfillment_status": "fulfilled",
                 "cancelled_at": None, "order_status_url": "https://shop/orders/abc"}
        cp = {"shipment_status": "Cancelled", "order_status": "Cancelled",
              "tracking_url": "", "awb": "", "courier": "DTDC B2C Surface L008", "edd": ""}
        with self._with_care_panel(cp):
            info = lookup_tracking(self.brand, "262098591",
                                   clients=self._clients(shopify=FakeShopify({"262098591": order})))
        self.assertEqual(info["status_source"], "care_panel_shipment")
        self.assertEqual(info["raw_status"], "Cancelled")
        self.assertIn("Status: Cancelled", service._format_tracking_details(info))

    def test_care_panel_statuses_shown_in_email(self):
        # Cancelled / NDR / RTO / Delivered / Out For Delivery / In Transit -> shown verbatim,
        # never overridden by Shopify fulfillment_status='fulfilled'.
        order = {"shipped": True, "raw_fulfillment_status": "fulfilled",
                 "financial_status": "paid", "cancelled_at": None}
        display = {"RTO": "Return To Origin (RTO)"}   # RTO is the only mapped label
        for status in ["Cancelled", "NDR", "RTO", "Delivered", "Out For Delivery", "In Transit"]:
            cp = {"shipment_status": status, "order_status": status, "tracking_url": "", "awb": ""}
            with self._with_care_panel(cp):
                info = lookup_tracking(self.brand, "486324",
                                       clients=self._clients(shopify=FakeShopify({"486324": order})))
            self.assertEqual(info["status_source"], "care_panel_shipment", status)
            self.assertEqual(info["raw_status"], status, status)   # raw_status kept verbatim
            shown = display.get(status, status)
            self.assertIn(f"Status: {shown}", service._format_tracking_details(info), status)

    def test_verification_orders_262098591_cancelled_262146052_ndr(self):
        # The two reported orders, end to end (Shopify says fulfilled for both).
        order = {"shipped": True, "raw_fulfillment_status": "fulfilled", "cancelled_at": None}
        for ref, expected in (("262098591", "Cancelled"), ("262146052", "NDR")):
            cp = {"shipment_status": expected, "order_status": expected, "tracking_url": "", "awb": ""}
            with self._with_care_panel(cp):
                info = lookup_tracking(self.brand, ref,
                                       clients=self._clients(shopify=FakeShopify({ref: order})))
            self.assertEqual(info["raw_status"], expected, ref)
            self.assertEqual(info["status_source"], "care_panel_shipment", ref)

    def test_care_panel_order_status_used_when_no_shipment_status(self):
        cp = {"shipment_status": "", "order_status": "In Transit", "tracking_url": "", "awb": ""}
        order = {"shipped": True, "raw_fulfillment_status": "fulfilled"}
        with self._with_care_panel(cp):
            info = lookup_tracking(self.brand, "486324",
                                   clients=self._clients(shopify=FakeShopify({"486324": order})))
        self.assertEqual(info["status_source"], "care_panel_order_status")
        self.assertEqual(info["raw_status"], "In Transit")

    def test_care_panel_status_never_overwritten_by_shopify(self):
        # Care Panel shipment status beats cancelled_at AND the courier status.
        cp = {"shipment_status": "RTO Delivered", "order_status": "", "tracking_url": "",
              "awb": "AWB9"}
        order = {"shipped": True, "awb": "AWB9", "raw_fulfillment_status": "fulfilled",
                 "cancelled_at": "2026-06-18T10:00:00Z"}
        shipping = FakeShipping({"AWB9": {"raw_status": "In Transit", "awb": "AWB9"}})
        with self._with_care_panel(cp):
            info = lookup_tracking(self.brand, "486324", clients=self._clients(
                shopify=FakeShopify({"486324": order}), shipping=shipping))
        self.assertEqual(info["status_source"], "care_panel_shipment")
        self.assertEqual(info["raw_status"], "RTO Delivered")

    def test_care_panel_null_tracking_url_falls_back_to_order_status(self):
        # Care Panel null trackingUrl (e.g. cancelled / not dispatched) -> fall back to the
        # Shopify order-status page so the customer ALWAYS gets a working link.
        cp = {"shipment_status": "Cancelled", "order_status": "", "tracking_url": "",
              "awb": "", "courier": "DTDC"}
        order = {"shipped": True, "order_status_url": "https://deodap.in/orders/abc"}
        with self._with_care_panel(cp):
            info = lookup_tracking(self.brand, "486324",
                                   clients=self._clients(shopify=FakeShopify({"486324": order})))
        self.assertEqual(info["tracking_url"], "https://deodap.in/orders/abc")
        self.assertEqual(info["tracking_link_source"], "shopify_order_status_url")
        self.assertIn("Track Order:", service._format_tracking_details(info))

    def test_care_panel_null_and_no_order_status_shows_no_link(self):
        # Null Care Panel trackingUrl AND no order-status URL / AWB -> no link (nothing to
        # link to); we never emit an internal/localhost link.
        cp = {"shipment_status": "Cancelled", "order_status": "", "tracking_url": "",
              "awb": "", "courier": "DTDC"}
        order = {"shipped": True, "order_status_url": ""}
        with self._with_care_panel(cp):
            info = lookup_tracking(self.brand, "486324",
                                   clients=self._clients(shopify=FakeShopify({"486324": order})))
        self.assertEqual(info["tracking_url"], "")
        self.assertEqual(info["tracking_link_source"], "none")

    def test_no_care_panel_falls_back_to_cancelled_then_fulfillment(self):
        # No Care Panel (returns None) -> the Shopify priority still holds.
        order = {"shipped": True, "awb": "", "raw_fulfillment_status": "fulfilled",
                 "cancelled_at": "2026-06-18T10:00:00Z"}
        with self._with_care_panel(None):
            info = lookup_tracking(self.brand, "486324",
                                   clients=self._clients(shopify=FakeShopify({"486324": order})))
        self.assertEqual(info["status_source"], "shopify_cancelled")
        self.assertEqual(info["raw_status"], "Cancelled")

    def test_cancelled_beats_courier_status(self):
        # cancelled_at present -> "Cancelled" even if a courier status also exists.
        body, info = self._status_email(courier_status="In Transit", fulfillment="fulfilled")
        self.assertEqual(info["status_source"], "courier")          # not cancelled here
        order = {"shipped": True, "awb": "AWB9", "tracking_url": "https://track/AWB9",
                 "cancelled_at": "2026-06-18T10:00:00Z", "raw_fulfillment_status": "fulfilled"}
        shipping = FakeShipping({"AWB9": {"status": "in_transit", "raw_status": "In Transit",
                                          "awb": "AWB9"}})
        info2 = lookup_tracking(self.brand, "486324",
                                clients=self._clients(shopify=FakeShopify({"486324": order}),
                                                      shipping=shipping))
        self.assertEqual(info2["status_source"], "shopify_cancelled")
        self.assertEqual(info2["raw_status"], "Cancelled")

    def test_not_cancelled_order_unaffected(self):
        # cancelled_at None -> normal RAW priority (fulfillment shown).
        body, info = self._status_email(fulfillment="fulfilled")
        self.assertEqual(info["status_source"], "shopify_fulfillment")
        self.assertIn("Status: fulfilled", body)

    def test_format_details_shows_raw_status_verbatim(self):
        # RAW STATUS: the exact courier/Shopify string is shown -- no mapping/title-casing.
        body = service._format_tracking_details(
            {"order_id": "262134021", "raw_status": "Out For Delivery", "courier": "DTDC",
             "awb": "7D130624612", "tracking_url": "https://ship.deodap.in/tracking/7D130624612"})
        self.assertIn("Order ID: 262134021", body)
        self.assertIn("Status: Out For Delivery", body)        # verbatim, NOT "Out for Delivery"
        self.assertIn("Courier: DTDC", body)
        self.assertIn("AWB: 7D130624612", body)
        self.assertIn("Track Order:\nhttps://ship.deodap.in/tracking/7D130624612", body)
        # missing fields don't render empty labels
        body2 = service._format_tracking_details({"order_id": "X1", "raw_status": "Manifested"})
        self.assertIn("Status: Manifested", body2)
        self.assertNotIn("Courier:", body2)
        self.assertNotIn("AWB:", body2)
        self.assertNotIn("Track Order:", body2)


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class TrackingFlowTests(TestCase):
    """End-to-end: 'where is my order' -> ask order -> reply order -> LIVE status, no ticket."""

    def setUp(self):
        from apps.brand_settings.models import BrandSettings
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        self.cat = Category.objects.create(brand=self.brand, code="1",
                                           name="Shipment & Delivery Tracking")
        self.sub = SubTopic.objects.create(category=self.cat, code="1.1",
                                            name="Shipment Status", mandatory_inputs=["order_id"])
        Rule.objects.create(sub_topic=self.sub, condition="Always", action=Rule.ACTION_INFO_ONLY,
                            then_response="track {tracking_url}")

    def _result(self):
        return ClassificationResult(
            category="1. Shipment & Delivery Tracking", sub_topic="1.1 Shipment Status",
            confidence=0.9, extracted={}, sentiment="neutral", language="en",
            is_support_request=True, issue_summary="where is my order",
            requires_evidence=False, requires_agent=False,
            category_ref=self.cat, sub_topic_ref=self.sub)

    def _run(self, *emails, clients):
        from apps.integrations import context as ctx, identity as idmod
        self.sent = []
        oc, oi, ob, oe = (service._classify_dict, idmod.resolve_identity,
                          ctx.build_clients, service._send_customer_email)
        service._classify_dict = lambda b, m: self._result()
        idmod.resolve_identity = lambda *a, **k: {
            "order": None, "orders": [], "needs_choice": False,
            "source": "none", "configured": True}
        ctx.build_clients = lambda settings: clients
        # Capture standalone customer emails (no-op via SMTP in test mode otherwise).
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append({"to": to, "subject": subject, "body": body}) or "<sent-id>")
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            (service._classify_dict, idmod.resolve_identity,
             ctx.build_clients, service._send_customer_email) = oc, oi, ob, oe

    def _last_out(self):
        return self.sent[-1] if self.sent else None

    def test_order_reply_returns_live_status_and_records_ticket(self):
        clients = {"shopify": FakeShopify({"486324": dict(ORDER)}),
                   "shipping": FakeShipping({"AWB123": dict(TRACK)}), "gokwik": None}
        self._run(
            eml(subject="where is my order", body="where is my order?", message_id="<a@x>"),
            eml(subject="Re: where is my order", body="order number : 486324",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>"),
            clients=clients)

        # A local AUTO-RESOLVED ticket is recorded (shows in the Tickets list) -- but the
        # customer still gets the instant status reply and NO Care Panel "created" mail.
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Ticket.objects.get().status, Ticket.STATUS_AUTO_RESOLVED)
        out = self._last_out()
        self.assertIn("Here is the latest status for your order:", out["body"])
        self.assertIn("Order ID: 486324", out["body"])
        self.assertIn("Status: In Transit", out["body"])
        self.assertIn("Courier: Delhivery", out["body"])
        self.assertIn("AWB: AWB123", out["body"])
        self.assertIn("Track Order:\nhttps://track/AWB123", out["body"])
        self.assertNotIn("looking into", out["body"].lower())     # no acknowledgement
        # IMPORTANT RULES: never a ticket id / Care Panel link / localhost
        self.assertNotIn("TKT-", out["body"])
        self.assertNotIn("care.deodap.in/t?id=", out["body"])
        self.assertNotIn("localhost", out["body"])
        self.assertFalse(Message.objects.filter(
            subject="Support Ticket Created Successfully").exists())

    def test_order_not_found_asks_to_verify_and_keeps_pending(self):
        clients = {"shopify": FakeShopify({}), "shipping": None, "gokwik": None}
        self._run(
            eml(subject="where is my order", body="where is my order?", message_id="<a@x>"),
            eml(subject="Re: where is my order", body="order number : 000000",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>"),
            clients=clients)
        self.assertEqual(Ticket.objects.count(), 0)
        out = self._last_out()
        self.assertIn("We could not locate an order using the provided details", out["body"])
        p = PendingConversation.objects.get()                       # still open
        self.assertEqual(p.order_id or "", "")                      # bad order cleared

    def test_tracking_unavailable_when_not_configured(self):
        clients = {"shopify": None, "shipping": None, "gokwik": None}
        self._run(
            eml(subject="where is my order", body="where is my order?", message_id="<a@x>"),
            eml(subject="Re: where is my order", body="order number : 486324",
                message_id="<a2@x>", in_reply_to="<a@x>", references="<a@x>"),
            clients=clients)
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertIn("unable to fetch your tracking", self._last_out()["body"])


ORDER_FULL = {"order_id": "486324", "shipped": True, "delivered": False,
              "edd": "18 Jun 2026", "tracking_url": "https://track/AWB123",
              "awb": "AWB123", "courier": "DTDC", "raw_fulfillment_status": "In Transit"}


@override_settings(PUBLIC_BASE_URL="https://care.deodap.in")
class ExplicitIdentifierTrackingTests(TestCase):
    """Required behavior: shipment tracking requires an ORDER NUMBER. Phone or email alone
    must NOT trigger a lookup -- they are acknowledged with a request for the order number;
    only an order number performs the Shopify lookup. Never a ticket."""

    def setUp(self):
        from apps.brand_settings.models import BrandSettings
        self.org = Organization.objects.create(name="DeoDap")
        self.brand = Brand.objects.create(organization=self.org, name="DeoDap.in")
        self.mailbox = Mailbox.objects.create(brand=self.brand, email_address="care@deodap.com")
        BrandSettings.objects.create(brand=self.brand, ai_api_key="k", confidence_threshold=0.75)
        self.cat = Category.objects.create(brand=self.brand, code="1",
                                           name="Shipment & Delivery Tracking")
        self.sub = SubTopic.objects.create(category=self.cat, code="1.1",
                                           name="Shipment Status", mandatory_inputs=["order_id"])
        Rule.objects.create(sub_topic=self.sub, condition="Always",
                            action=Rule.ACTION_INFO_ONLY, then_response="track {tracking_url}")
        self.shop = FakeShopify(
            orders={"486324": dict(ORDER_FULL)},
            by_phone={"9876543210": [dict(ORDER_FULL)]},
            by_email={"registered@shop.com": [dict(ORDER_FULL)]})

    def _result(self):
        return ClassificationResult(
            category="1. Shipment & Delivery Tracking", sub_topic="1.1 Shipment Status",
            confidence=0.9, extracted={}, sentiment="neutral", language="en",
            is_support_request=True, issue_summary="where is my order",
            requires_evidence=False, requires_agent=False,
            category_ref=self.cat, sub_topic_ref=self.sub)

    def _run(self, *emails):
        from apps.integrations import context as ctx
        self.sent = []
        oc, ob, oe = service._classify_dict, ctx.build_clients, service._send_customer_email
        service._classify_dict = lambda b, m: self._result()
        ctx.build_clients = lambda settings: {
            "shopify": self.shop, "shipping": None, "gokwik": None}
        service._send_customer_email = lambda to, subject, body, **k: (
            self.sent.append({"to": to, "subject": subject, "body": body}) or "<sent>")
        try:
            for i, e in enumerate(emails):
                service.fetch_imap(self.mailbox, client=FakeImap([e], start_uid=i + 1))
        finally:
            service._classify_dict, ctx.build_clients, service._send_customer_email = oc, ob, oe

    def _last(self):
        return self.sent[-1] if self.sent else None

    def _first_email(self, mid="<a@x>"):
        return eml(subject="where is my order", body="Hi, where is my order?", message_id=mid)

    def _reply(self, body, mid="<a2@x>"):
        return eml(subject="Re: where is my order", body=body, message_id=mid,
                   in_reply_to="<a@x>", references="<a@x>")

    def _assert_no_forbidden(self, body):
        for bad in ("TKT-", "care.deodap.in/t?id=", "localhost", "127.0.0.1"):
            self.assertNotIn(bad, body, f"forbidden {bad!r} in: {body!r}")

    # STEP 2: where is my order (no identifier) -> ask for ANY ONE, NO Shopify call -----
    def test_first_email_no_identifier_asks_for_any_one(self):
        self._run(self._first_email())
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertEqual(PendingConversation.objects.count(), 1)
        body = self._last()["body"]
        self.assertIn("Thank you for contacting DeoDap", body)
        self.assertIn("ANY ONE of the following", body)
        self.assertIn("Order Number", body)
        self.assertIn("Registered Mobile Number", body)
        self.assertIn("Registered Email ID", body)
        self.assertEqual(self.shop.calls, [])                  # NO Shopify lookup at this stage

    # STEP 4A + 5: order number -> Shopify get_order -> live status --------------------
    def test_order_id_reply_returns_status(self):
        self._run(self._first_email(), self._reply("my order id is 486324"))
        body = self._last()["body"]
        self.assertIn("Order ID: 486324", body)
        self.assertIn("Status: In Transit", body)
        self.assertIn("AWB: AWB123", body)
        self.assertIn(("get_order", "486324"), self.shop.calls)
        self.assertEqual(Ticket.objects.count(), 1)                       # auto-resolved record
        self.assertEqual(Ticket.objects.get().status, Ticket.STATUS_AUTO_RESOLVED)
        self._assert_no_forbidden(body)

    # STEP 4B + 5: phone -> Shopify recent_orders_by_phone -> live status --------------
    def test_phone_reply_returns_status(self):
        self._run(self._first_email(), self._reply("my mobile is 9876543210"))
        body = self._last()["body"]
        self.assertIn("Status: In Transit", body)
        self.assertIn("Order ID: 486324", body)
        self.assertIn(("by_phone", "9876543210"), self.shop.calls)
        self.assertEqual(Ticket.objects.count(), 1)                       # auto-resolved record
        self.assertEqual(Ticket.objects.get().status, Ticket.STATUS_AUTO_RESOLVED)
        self._assert_no_forbidden(body)

    # STEP 4C + 5: email -> Shopify recent_orders_by_email -> live status --------------
    def test_email_reply_returns_status(self):
        self._run(self._first_email(),
                  self._reply("my registered email is registered@shop.com"))
        body = self._last()["body"]
        self.assertIn("Status: In Transit", body)
        self.assertIn(("by_email", "registered@shop.com"), self.shop.calls)
        self.assertEqual(Ticket.objects.count(), 1)                       # auto-resolved record
        self.assertEqual(Ticket.objects.get().status, Ticket.STATUS_AUTO_RESOLVED)
        self._assert_no_forbidden(body)

    # The recorded auto-resolved ticket carries the category + live tracking facts, and
    # makes NO external Care Panel store call (Route A: local record only).
    def test_successful_tracking_records_autoresolved_ticket_no_store(self):
        from unittest import mock
        with mock.patch("apps.integrations.care_panel_store.store_ticket") as store:
            self._run(self._first_email(), self._reply("my order id is 486324"))
        store.assert_not_called()                                  # never the external store
        t = Ticket.objects.get()
        self.assertEqual(t.status, Ticket.STATUS_AUTO_RESOLVED)
        self.assertTrue(t.ai_handled)
        self.assertEqual(t.sub_topic, "1.1 Shipment Status")       # category recorded
        self.assertEqual((t.extracted or {}).get("order_id"), "486324")
        self.assertEqual((t.extracted or {}).get("awb"), "AWB123")
        self.assertEqual(t.tracking_url, "")                       # no internal/care.deodap link

    # STEP 6: identifier not found -> verify-and-resend, pending kept open --------------
    def test_not_found_replies_step6_and_keeps_open(self):
        self.shop.orders = {}                                   # nothing matches
        self._run(self._first_email(), self._reply("order id 999999"))
        body = self._last()["body"]
        self.assertIn("We could not locate an order using the provided details", body)
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertEqual(PendingConversation.objects.filter(status="closed").count(), 0)

    # UNIVERSAL RULE: a FIRST email that ALREADY contains a valid identifier is verified +
    # answered immediately (no M_TRACK_LOOKUP ask). An invalid one -> not-found ask.
    def _assert_is_lookup_ask(self, body):
        self.assertIn("Thank you for contacting DeoDap", body)
        self.assertIn("ANY ONE of the following", body)
        self.assertNotIn("Here is the latest status", body)

    def test_first_email_with_valid_email_sends_status(self):
        self._run(eml(subject="Where is my order?",
                      body="Where is my order? My email is registered@shop.com",
                      message_id="<a@x>"))
        body = self._last()["body"]
        self.assertIn("Here is the latest status", body)          # status, NOT a lookup ask
        self.assertIn(("by_email", "registered@shop.com"), self.shop.calls)
        self.assertEqual(Ticket.objects.get().status, Ticket.STATUS_AUTO_RESOLVED)

    def test_first_email_with_valid_phone_sends_status(self):
        self._run(eml(subject="Where is my order?",
                      body="Where is my order? My phone number is 9876543210",
                      message_id="<a@x>"))
        body = self._last()["body"]
        self.assertIn("Here is the latest status", body)
        self.assertIn("Order ID: 486324", body)
        self.assertIn(("by_phone", "9876543210"), self.shop.calls)
        self.assertEqual(Ticket.objects.get().status, Ticket.STATUS_AUTO_RESOLVED)

    def test_first_email_with_valid_order_sends_status(self):
        self._run(eml(subject="Where is my order?",
                      body="Where is my order? Order number 486324", message_id="<a@x>"))
        body = self._last()["body"]
        self.assertIn("Here is the latest status", body)
        self.assertIn("Order ID: 486324", body)
        self.assertIn(("get_order", "486324"), self.shop.calls)

    def test_first_email_with_invalid_order_sends_not_found(self):
        self._run(eml(subject="Where is my order?",
                      body="Where is my order? Order number 999999", message_id="<a@x>"))
        body = self._last()["body"]
        self.assertIn("We could not locate an order using the provided details", body)
        self.assertNotIn("Here is the latest status", body)
        self.assertEqual(Ticket.objects.count(), 0)               # no match -> no ticket
        self.assertEqual(PendingConversation.objects.get().status, "awaiting_evidence")  # open

    def test_first_email_no_identifier_asks(self):
        self._run(self._first_email())
        self._assert_is_lookup_ask(self._last()["body"])
        self.assertEqual(self.shop.calls, [])                     # nothing to look up
        self.assertEqual(Ticket.objects.count(), 0)

    def test_first_email_with_identifier_dedups(self):
        # Same Message-ID twice -> the status is sent only ONCE (the first email recorded
        # its Message-ID via the auto-resolved ticket, so the re-fetch is deduped).
        em = eml(subject="Where is my order?",
                 body="Where is my order?\nMy phone number is 9876543210", message_id="<dup@x>")
        self._run(em, em)
        statuses = [s for s in self.sent if "Here is the latest status" in s["body"]]
        self.assertEqual(len(statuses), 1)                        # ONE status, deduped
        self.assertEqual(Ticket.objects.count(), 1)               # ONE auto-resolved ticket

    # Keep the SAME conversation open until a valid identifier is received -------------
    def test_one_pending_until_valid_identifier(self):
        self._run(self._first_email(),                                    # ask
                  self._reply("order id 999999", mid="<a2@x>"),           # not found -> open
                  self._reply("order id 486324", mid="<a3@x>"))           # found -> status
        self.assertEqual(PendingConversation.objects.count(), 1)          # never duplicated
        self.assertEqual(Ticket.objects.count(), 1)                       # one auto-resolved record
        self.assertEqual(Ticket.objects.get().status, Ticket.STATUS_AUTO_RESOLVED)
        self.assertIn("Status: In Transit", self._last()["body"])
        self.assertEqual(PendingConversation.objects.get().status, "closed")  # closed after status
