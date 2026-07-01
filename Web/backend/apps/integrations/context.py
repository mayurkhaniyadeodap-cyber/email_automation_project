"""
Build the live-data `context` the decision engine evaluates conditions against
(doc section 5). Given a ticket, look up its order / shipment / payment from the
configured integrations and flatten the results into the fact keys the engine's
condition evaluator understands:

    shipped, dispatched, delivered, edd, edd_breached, custom_item,
    tracking_url, double_payment, paid, financial_status

Everything is best-effort: a missing integration or a lookup error simply leaves
the fact absent, which keeps the related condition "unevaluable" so the engine
drafts instead of auto-answering on stale data.
"""

import datetime
import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def _settings_for(brand):
    from apps.brand_settings.models import BrandSettings

    try:
        return brand.settings
    except BrandSettings.DoesNotExist:
        return None


def _edd_breached(edd_value):
    """True if the expected delivery date is in the past. None if unknown/unparsable."""
    if not edd_value:
        return None
    text = str(edd_value)
    for parse in (datetime.date.fromisoformat, lambda s: datetime.datetime.fromisoformat(s).date()):
        try:
            edd_date = parse(text[:19])
            return edd_date < timezone.localdate()
        except ValueError:
            continue
    return None


def build_clients(settings):
    """Indirection so tests can monkeypatch client construction."""
    from .clients import build_clients as _build

    return _build(settings)


def build_context(ticket, clients=None):
    """Return a flat facts dict for the engine. Never raises."""
    brand = ticket.brand
    if clients is None:
        clients = build_clients(_settings_for(brand))

    extracted = ticket.extracted or {}
    order_id = extracted.get("order_id")
    awb = extracted.get("awb")
    facts = {}

    shopify = clients.get("shopify")
    if shopify and order_id:
        try:
            order = shopify.get_order(order_id)
            if order:
                for key in ("shipped", "dispatched", "delivered", "edd",
                            "tracking_url", "custom_item", "financial_status"):
                    if order.get(key) not in (None, ""):
                        facts[key] = order[key]
                if order.get("awb") and not awb:
                    awb = order["awb"]
        except Exception:  # noqa: BLE001 -- best-effort
            logger.exception("Shopify lookup failed for %s", ticket.ticket_id)

    shipping = clients.get("shipping")
    if shipping and awb:
        try:
            tracking = shipping.track(awb)
            if tracking:
                # Live tracking wins over Shopify's coarse fulfillment flags.
                for key in ("delivered", "shipped", "edd", "tracking_url"):
                    if tracking.get(key) not in (None, ""):
                        facts[key] = tracking[key]
        except Exception:  # noqa: BLE001
            logger.exception("Shipping lookup failed for %s", ticket.ticket_id)

    gokwik = clients.get("gokwik")
    if gokwik and order_id:
        try:
            payment = gokwik.get_payment(order_id)
            if payment:
                for key in ("paid", "double_payment", "amount"):
                    if payment.get(key) is not None:
                        facts[key] = payment[key]
        except Exception:  # noqa: BLE001
            logger.exception("GoKwik lookup failed for %s", ticket.ticket_id)

    breached = _edd_breached(facts.get("edd"))
    if breached is not None:
        facts["edd_breached"] = breached

    # Evidence flags the engine's await-evidence rule (and the no-re-ask guard) consult.
    # MUST be in the context or the engine thinks no photo/video was received and re-asks
    # for evidence we already have -- creating a ticket AND an evidence request for one mail.
    for flag in ("has_photo", "has_unboxing_video", "has_video"):
        if extracted.get(flag):
            facts[flag] = True
    if facts.get("has_photo") or facts.get("has_unboxing_video") or facts.get("has_video"):
        logger.info("EVIDENCE-PRESENT ticket=%s has_photo=%s has_unboxing_video=%s",
                    ticket.ticket_id, bool(facts.get("has_photo")),
                    bool(facts.get("has_unboxing_video") or facts.get("has_video")))

    return facts


def compute_refund_status(financial_status="", raw_status="", cancelled_at=None):
    """Customer-facing refund status from Shopify financial data (+ the RTO/cancel signal):
    Refunded | Partially Refunded | Refund In Progress | Not Refunded | Not Applicable."""
    fin = (financial_status or "").lower()
    raw = (raw_status or "").lower()
    if fin == "refunded":
        return "Refunded"
    if fin == "partially_refunded":
        return "Partially Refunded"
    if fin == "voided":
        return "Not Applicable"
    # Return To Origin -> the shipment is coming back; refund is assessed after it reaches the
    # warehouse (unless it was already refunded above).
    if raw.startswith("rto") or "return to origin" in raw:
        return "Pending verification after returned shipment reaches the warehouse."
    # A returned / cancelled order: the money is owed back but not yet refunded.
    returned = bool(cancelled_at) or any(k in raw for k in ("rto", "return", "cancel", "refund"))
    if returned:
        return "Refund In Progress" if fin in ("paid", "partially_paid") else "Not Refunded"
    return "Not Applicable"          # a normal paid/delivered order -> no refund expected


def _care_panel_only_tracking(brand, order_id, phone, email, out):
    """Shopify had NO match -> resolve tracking from the Care Panel shipment API alone (seller /
    marketplace orders live there, not in Shopify). Populates `out` with the real shipment status
    / AWB / courier / tracking link when found; otherwise returns `out` unchanged."""
    from django.conf import settings as dj_settings

    from apps.integrations import care_panel

    cp = care_panel.fetch_shipment_flow(brand, order_id, phone=phone, email=email)
    status = (cp or {}).get("shipment_status") or (cp or {}).get("order_status") or ""
    if not cp or not (status or cp.get("awb") or cp.get("tracking_url")):
        return out                                   # Care Panel also has nothing -> no tracking

    out["found"] = True
    out["matched_by"] = "care_panel"
    out["matched_identifier"] = order_id or phone or email
    out["order_id"] = order_id or ""
    out["awb"] = cp.get("awb") or ""
    out["courier"] = cp.get("courier") or ""
    out["edd"] = cp.get("edd") or ""
    out["care_panel_called"] = True
    out["panel_status"] = status
    out["raw_status"], out["status_source"] = status, "care_panel_shipment"
    url = (cp.get("tracking_url") or "").strip()
    if not url and out["awb"]:
        base = getattr(dj_settings, "SHIPPING_TRACKING_URL_BASE",
                       "https://ship.deodap.in/tracking/").rstrip("/")
        url = f"{base}/{out['awb']}"
    out["tracking_url"] = url
    out["tracking_link_source"] = "care_panel" if cp.get("tracking_url") else (
        "awb" if out["awb"] else "none")
    low = status.lower()
    out["delivered"] = "deliver" in low
    out["shipped"] = out["delivered"] or any(k in low for k in
                                             ("transit", "shipped", "dispatch", "out for"))
    out["status"] = ("delivered" if out["delivered"]
                     else "in_transit" if out["shipped"] else "processing")
    out["refund_status"] = compute_refund_status(raw_status=status)
    logger.info("ORDER_ID=%s", order_id or "-")
    logger.info("REFUND_STATUS=%s", out["refund_status"])
    logger.info("CARE-PANEL-FALLBACK-MATCH order=%s status=%s awb=%s url=%s",
                order_id or "-", status, out["awb"] or "-", out["tracking_url"] or "-")
    return out


def lookup_tracking(brand, order_id="", awb="", clients=None, phone="", email=""):
    """Live order-tracking lookup for the Shipment-Tracking flow, by ANY ONE identifier
    the customer explicitly provided -- order number, registered phone, or registered
    email (NEVER the sender's From address):
        Shopify order -> fulfillment (AWB / courier / tracking url) -> courier track(AWB).

    Returns a structured outcome (never raises):
      configured : a Shopify client is available to look the order up
      found      : an order was resolved (False = configured but not found -> verify)
      error      : a lookup call failed -> tracking temporarily unavailable
      order_id   : the resolved order number (useful when looked up by phone/email)
      status / courier / awb / edd / tracking_url / shipped / delivered : the live data
    """
    if clients is None:
        clients = build_clients(_settings_for(brand))
    shopify = clients.get("shopify")
    out = {"configured": bool(shopify), "found": False, "error": False,
           "order_id": order_id or "", "status": "", "raw_status": "", "status_source": "",
           "courier": "", "awb": awb or "",
           "edd": "", "tracking_url": "", "tracking_link_source": "none",
           "matched_by": "", "matched_identifier": "", "panel_status": "", "customer_name": "",
           "customer_phone": "", "customer_email": "",
           "refund_status": "Not Applicable",
           "shipped": None, "delivered": None}
    if not shopify or not (order_id or phone or email):
        return out

    # OR logic: try EVERY provided identifier (order -> mobile -> email) and stop at the
    # FIRST that resolves a Shopify order. A non-matching order number must NOT prevent the
    # mobile/email from matching -- that was the bug where a valid identifier still failed.
    order, matched_by, matched_value = None, "", ""
    try:
        if order_id:
            order = shopify.get_order(order_id)
            if order:
                matched_by, matched_value = "order_id", order_id
                logger.info("ORDER-FOUND order_id=%s", order_id)
        if order is None and phone:
            orders = shopify.recent_orders_by_phone(phone, limit=5)
            if orders:
                order, matched_by, matched_value = orders[0], "mobile", phone
                logger.info("PHONE-FOUND mobile=%s", phone)
        if order is None and email:
            orders = shopify.recent_orders_by_email(email, limit=5)
            if orders:
                order, matched_by, matched_value = orders[0], "email", email
                logger.info("EMAIL-FOUND email=%s", email)
    except Exception:  # noqa: BLE001 -- best-effort
        logger.exception("Tracking: Shopify lookup failed (order=%s phone=%s email=%s)",
                         order_id or "-", phone or "-", email or "-")
        out["error"] = True
        return out
    if not order:
        logger.info("SHOPIFY-NO-MATCH order=%s phone=%s email=%s",
                    order_id or "-", phone or "-", email or "-")
        # FALLBACK: seller / marketplace orders (e.g. a 6-digit club-order number) are NOT in the
        # Shopify store, so Shopify can't resolve them. Ask the Care Panel shipment API directly --
        # it has these orders + their AWB / courier / status -- so the customer still gets real
        # tracking instead of a generic reply.
        return _care_panel_only_tracking(brand, order_id, phone, email, out)

    out["found"] = True
    out["matched_by"], out["matched_identifier"] = matched_by, matched_value
    out["order_id"] = order.get("order_id") or order.get("name") or order_id or ""
    out["customer_name"] = (order.get("customer_name") or "").strip()
    out["customer_phone"] = (order.get("customer_phone") or "").strip()
    out["customer_email"] = (order.get("customer_email") or "").strip()
    logger.info("SHOPIFY-MATCH verified_by=%s identifier=%s resolved_order=%s",
                matched_by, matched_value, out["order_id"])
    logger.info("VERIFIED-ORDER-ID %s", out["order_id"])
    logger.info("SHOPIFY-FIRST-NAME %s", order.get("customer_first_name") or "-")
    logger.info("SHOPIFY-LAST-NAME %s", order.get("customer_last_name") or "-")
    logger.info("SHOPIFY-CUSTOMER-NAME %s", out["customer_name"] or "-")
    out["shipped"] = order.get("shipped")
    out["delivered"] = order.get("delivered")
    out["edd"] = order.get("edd") or ""
    out["tracking_url"] = order.get("tracking_url") or ""
    out["awb"] = order.get("awb") or awb or ""
    out["courier"] = order.get("courier") or ""

    # === (PRIMARY) Care Panel shipment-flow API -- shipment.status / tracking.orderStatus ===
    # This is the authoritative tracking source: it reads 'Cancelled' for an order Shopify
    # still reports 'fulfilled'. We also take its trackingUrl / awb / courier / edd.
    from apps.integrations import care_panel

    cp = care_panel.fetch_shipment_flow(brand, out["order_id"], phone=phone, email=email)
    cp_shipment_status = (cp or {}).get("shipment_status") or ""
    cp_order_status = (cp or {}).get("order_status") or ""
    if cp:
        out["awb"] = cp.get("awb") or out["awb"]
        out["courier"] = cp.get("courier") or out["courier"]
        out["edd"] = cp.get("edd") or out["edd"]
    out["panel_status"] = cp_shipment_status or cp_order_status
    out["care_panel_called"] = cp is not None
    logger.info("CARE-PANEL-SHIPMENT-STATUS %s", cp_shipment_status or "-")
    logger.info("CARE-PANEL-ORDER-STATUS %s", cp_order_status or "-")

    # Live courier tracking by AWB -- a LOWER-priority status, never overwrites Care Panel.
    shipping = clients.get("shipping")
    courier_status = ""
    if shipping and out["awb"]:
        try:
            t = shipping.track(out["awb"]) or {}
            courier_status = (t.get("raw_status") or t.get("status") or "").strip()
            out["status"] = t.get("status") or out["status"]
            out["courier"] = t.get("courier") or out["courier"]
            out["edd"] = t.get("edd") or out["edd"]
            if not cp:
                out["tracking_url"] = t.get("tracking_url") or out["tracking_url"]
            if t.get("shipped") is not None:
                out["shipped"] = t["shipped"]
            if t.get("delivered") is not None:
                out["delivered"] = t["delivered"]
        except Exception:  # noqa: BLE001
            # The AWB courier track is LOWER-priority enrichment now -- a failure (e.g. a
            # mis-pointed SHIPPING_BASE_URL) must NOT mark the whole lookup errored. Care
            # Panel / Shopify remain authoritative; we simply skip the courier status.
            logger.warning("Tracking: courier lookup failed for awb=%s -> skipped (non-fatal).",
                           out["awb"])

    # === TRACK-ORDER LINK -- always provide a link when one can be built (priority): ===
    #   1 Care Panel courier trackingUrl  2 Shopify fulfillment tracking_url (only when Care
    #   Panel had NO data)  3 Shopify order-status page (works even for cancelled/refunded)
    #   4 built-from-AWB. We never emit an internal/localhost link. A cancelled order with no
    #   courier yet still gets the order-status link (was: no link at all).
    from django.conf import settings as dj_settings

    cp_url = ((cp or {}).get("tracking_url") or "").strip()
    shopify_courier_url = (order.get("tracking_url") or "").strip()
    order_status_url = (order.get("order_status_url") or "").strip()
    awb = (out.get("awb") or "").strip()
    if cp_url:
        out["tracking_url"], out["tracking_link_source"] = cp_url, "care_panel"
    elif shopify_courier_url and cp is None:
        # Trust Shopify's own fulfillment tracking only when Care Panel returned nothing.
        out["tracking_url"], out["tracking_link_source"] = shopify_courier_url, "courier"
    elif order_status_url:
        out["tracking_url"], out["tracking_link_source"] = order_status_url, "shopify_order_status_url"
        if cp is not None:
            logger.info("TRACKING-LINK-CAREPANEL (null) -> order-status fallback.")
    elif awb:
        base = getattr(dj_settings, "SHIPPING_TRACKING_URL_BASE",
                       "https://ship.deodap.in/tracking/").rstrip("/")
        out["tracking_url"], out["tracking_link_source"] = f"{base}/{awb}", "awb"
    else:
        out["tracking_url"], out["tracking_link_source"] = "", "none"
    logger.info("TRACKING-LINK-FINAL %s (source=%s)",
                out["tracking_url"] or "-", out["tracking_link_source"])

    # === CUSTOMER STATUS resolution (shown VERBATIM). Care Panel is PRIMARY. Priority:
    #   1 Care Panel shipment.status  2 Care Panel tracking.orderStatus  3 Shopify cancelled_at
    #   4 Shopify financial_status (refund/void only -- never 'paid')    5 courier status
    #   6 Shopify fulfillment_status  7 Shopify order status.
    # A Care Panel status is NEVER overwritten by Shopify data. ===
    cancelled_at = order.get("cancelled_at")
    fulfillment_raw = (order.get("raw_fulfillment_status") or "").strip()
    financial_raw = (order.get("financial_status") or order.get("raw_order_status") or "").strip()
    order_raw = (order.get("raw_order_status") or "").strip()
    out["cancelled_at"] = cancelled_at
    out["cancel_reason"] = order.get("cancel_reason") or ""
    out["order_status_url"] = order.get("order_status_url") or ""
    logger.info("SHOPIFY-FULFILLMENT-STATUS %s", fulfillment_raw or "-")
    logger.info("SHOPIFY-FINANCIAL-STATUS %s", financial_raw or "-")
    logger.info("SHOPIFY-CANCELLED-AT %s", cancelled_at or "-")
    logger.info("COURIER-STATUS %s", courier_status or "-")
    # financial_status is only customer-facing for terminal refund/void states.
    financial_terminal = financial_raw if financial_raw.lower() in (
        "refunded", "partially_refunded", "voided") else ""
    if cp_shipment_status:
        out["raw_status"], out["status_source"] = cp_shipment_status, "care_panel_shipment"
    elif cp_order_status:
        out["raw_status"], out["status_source"] = cp_order_status, "care_panel_order_status"
    elif cancelled_at:
        out["raw_status"], out["status_source"] = "Cancelled", "shopify_cancelled"
    elif financial_terminal:
        out["raw_status"], out["status_source"] = financial_terminal, "shopify_financial"
    elif courier_status:
        out["raw_status"], out["status_source"] = courier_status, "courier"
    elif fulfillment_raw:
        out["raw_status"], out["status_source"] = fulfillment_raw, "shopify_fulfillment"
    elif order_raw:
        out["raw_status"], out["status_source"] = order_raw, "shopify_order"
    else:
        out["raw_status"], out["status_source"] = "", "none"
    logger.info("STATUS-SOURCE %s", out["status_source"])
    logger.info("FINAL-CUSTOMER-STATUS %s (source=%s)", out["raw_status"] or "-",
                out["status_source"])

    # Internal-only normalized status (drives shipped/delivered-style flags, NEVER shown to
    # the customer). The customer always sees out['raw_status'] verbatim.
    if not out["status"]:
        out["status"] = ("delivered" if out["delivered"]
                         else "in_transit" if out["shipped"] else "processing")

    # REFUND STATUS (shown on tracking / order-status / RTO emails) from Shopify financial data.
    out["refund_status"] = compute_refund_status(
        financial_status=order.get("financial_status") or "",
        raw_status=out["raw_status"], cancelled_at=cancelled_at)
    logger.info("ORDER_ID=%s", out["order_id"] or "-")
    logger.info("REFUND_STATUS=%s", out["refund_status"])
    return out
