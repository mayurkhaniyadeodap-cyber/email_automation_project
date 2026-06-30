"""
External DeoDap Care Panel "Open Ticket" API (care.deodap.info).

Single endpoint: POST {base}/open-tickets  (find/decision, keyed on customer PHONE)

    Request : {"phone": "...", "email": "...", "order_id": "..."}
    Response: {"success": true,
               "action": "no_open_tickets" | "await_customer_phone" | ...,
               "hasTickets": <bool>, "ticketCount": <int>, "tickets": [...],
               "reply": "<message>"}

We call it when finalizing a ticket: if it returns open tickets for this customer
(phone/email/order) we MATCH and do not create a duplicate; otherwise it's a new
ticket. `phone` is required by the API -- extracted from the email (apps.classifier).

Configurable per brand (BrandSettings.integrations["care_panel"]) or globally via
settings.CARE_PANEL_API_URL / CARE_PANEL_API_KEY. Auth: x-api-key header.
"""

import logging

from django.conf import settings as dj

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15


def _requests():
    import requests

    return requests


class CarePanelClient:
    def __init__(self, base_url, api_key):
        base = (base_url or "").rstrip("/")
        if base.endswith("/open-tickets"):
            base = base[: -len("/open-tickets")]
        self.base_url = base
        self.api_key = api_key

    @property
    def _headers(self):
        return {"x-api-key": self.api_key, "Accept": "application/json",
                "Content-Type": "application/json"}

    def lookup(self, *, phone=None, email=None, order_id=None):
        """POST the open-tickets lookup. Returns the parsed response dict.
        Raises if phone is missing (the API requires it)."""
        if not phone:
            raise ValueError("Care Panel lookup requires a customer phone number.")
        payload = {"phone": phone}
        if email:
            payload["email"] = email
        if order_id:
            payload["order_id"] = order_id
        r = _requests().post(f"{self.base_url}/open-tickets", headers=self._headers,
                             json=payload, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.json()


def _settings_for(brand):
    from apps.brand_settings.models import BrandSettings

    try:
        return brand.settings
    except BrandSettings.DoesNotExist:
        return None


def build_client(settings_obj):
    cfg = ((settings_obj.integrations if settings_obj else None) or {}).get("care_panel") or {}
    base_url = cfg.get("base_url") or getattr(dj, "CARE_PANEL_API_URL", "")
    api_key = cfg.get("api_key") or getattr(dj, "CARE_PANEL_API_KEY", "")
    if not base_url or not api_key:
        return None
    return CarePanelClient(base_url, api_key)


def build_client_for(brand):
    return build_client(_settings_for(brand))


# --------------------------------------------------------------------------- #
# Care Panel SHIPMENT-FLOW API -- the PRIMARY tracking-status source.
# --------------------------------------------------------------------------- #

def _shipment_auth(brand):
    """(url, api_key, token) for the shipment-flow API. Per-brand care_panel cfg first."""
    cfg = ((_settings_for(brand).integrations if _settings_for(brand) else None) or {}) \
        .get("care_panel") or {}
    url = cfg.get("shipment_url") or getattr(dj, "CARE_PANEL_SHIPMENT_URL", "")
    api_key = cfg.get("api_key") or getattr(dj, "CARE_PANEL_API_KEY", "")
    token = getattr(dj, "CARE_PANEL_TOKEN", "") or getattr(dj, "CARE_PANEL_STORE_TOKEN", "")
    return url, api_key, token


def _normalize_shipment_flow(data):
    """Flatten the shipment-flow response to the fields we use. Robust to nesting under
    'data' / 'shipment' / 'tracking' and to camelCase / snake_case keys."""
    if not isinstance(data, dict):
        return None
    root = data.get("data") if isinstance(data.get("data"), dict) else data
    shipment = root.get("shipment") if isinstance(root.get("shipment"), dict) else {}
    tracking = root.get("tracking") if isinstance(root.get("tracking"), dict) else {}

    def pick(*keys):
        for src in (shipment, tracking, root):
            for k in keys:
                v = src.get(k)
                if v not in (None, "", [], {}):
                    return str(v).strip()
        return ""

    return {
        "shipment_status": (shipment.get("status") or root.get("shipmentStatus") or "").strip(),
        "order_status": (tracking.get("orderStatus") or tracking.get("order_status")
                         or root.get("orderStatus") or "").strip(),
        "tracking_url": pick("trackingUrl", "tracking_url", "url"),
        "awb": pick("awb", "awbNumber", "awb_number", "tracking_number", "trackingNumber"),
        "courier": pick("courier", "courierName", "courier_name", "carrier"),
        "edd": pick("edd", "expectedDelivery", "expected_delivery", "estimatedDelivery"),
    }


def fetch_shipment_flow(brand, order_id=None, *, phone=None, email=None):
    """Call the Care Panel shipment-flow API. Returns the normalized dict (see
    _normalize_shipment_flow) or None if not configured / the call failed. Best-effort:
    NEVER raises. Logs CARE-PANEL-API-CALLED with the outcome."""
    url, api_key, token = _shipment_auth(brand)
    if not url or not api_key or not (order_id or phone or email):
        logger.info("CARE-PANEL-API-CALLED order=%s configured=%s -> SKIPPED (no url/key/id)",
                    order_id or "-", bool(url and api_key))
        return None

    import json as _json

    headers = {"x-api-key": api_key, "Accept": "application/json",
               "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # EXACT request the shipment-flow API requires: track by order_no, refNo = the order id
    # (bare number, no leading '#'). Was wrongly {order_id, phone, email} -> the API returned
    # nothing -> the status fell back to Shopify 'fulfilled'.
    ref_no = str(order_id or "").lstrip("#").strip()
    payload = {"topic": "shipment_status", "trackWith": "order_no", "refNo": ref_no}
    logger.info("CARE-PANEL-API-CALLED order=%s url=%s", ref_no, url)
    logger.info("CARE-PANEL-REQUEST %s", _json.dumps(payload))
    try:
        r = _requests().post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
        raw_text = r.text
        logger.info("CARE-PANEL-RAW-RESPONSE order=%s http=%s body=%s",
                    ref_no, r.status_code, (raw_text or "")[:1500])
        r.raise_for_status()
        data = r.json() or {}
    except Exception as exc:  # noqa: BLE001 -- best-effort; status falls back to Shopify
        logger.warning("CARE-PANEL-API-CALLED order=%s -> ERROR %s (falling back to Shopify)",
                       ref_no, exc)
        return None
    norm = _normalize_shipment_flow(data)
    logger.info("CARE-PANEL-API-CALLED order=%s -> OK shipment_status=%s order_status=%s "
                "tracking_url=%s awb=%s courier=%s edd=%s", ref_no,
                (norm or {}).get("shipment_status") or "-", (norm or {}).get("order_status") or "-",
                (norm or {}).get("tracking_url") or "-", (norm or {}).get("awb") or "-",
                (norm or {}).get("courier") or "-", (norm or {}).get("edd") or "-")
    return norm


def sync_ticket(ticket, client=None):
    """Check the external Care Panel for an existing open ticket for this customer
    (phone/email/order). MATCH -> store the external id + audit; otherwise record
    that no open ticket exists (the local ticket stands as the new one). Best-effort.
    """
    from apps.tickets.models import AuditLogEntry

    if client is None:
        client = build_client_for(ticket.brand)
    if client is None:
        return None

    extracted = ticket.extracted or {}
    phone = extracted.get("phone")
    if not phone:
        # The API is phone-keyed; without a phone we can't query it.
        AuditLogEntry.objects.create(
            ticket=ticket, actor="system", event="care_panel_skipped",
            detail={"reason": "no_phone"},
        )
        return None

    try:
        resp = client.lookup(
            phone=phone, email=ticket.customer_email, order_id=extracted.get("order_id"),
        )
    except Exception:  # noqa: BLE001 -- best-effort
        logger.exception("Care Panel open-tickets lookup FAILED for %s", ticket.ticket_id)
        return None

    logger.info("Care Panel open-tickets RESPONSE ticket=%s hasTickets=%s count=%s action=%s",
                ticket.ticket_id, resp.get("hasTickets"), resp.get("ticketCount"),
                resp.get("action"))

    tickets = resp.get("tickets") or []
    if resp.get("hasTickets") and tickets:
        # Only match an open ticket that is the SAME ISSUE (and same order when known).
        # A different issue (e.g. Defective vs Urgent Delivery) must NOT be linked --
        # it becomes a NEW ticket (rule: different issue = new ticket).
        from apps.integrations.care_panel_store import resolve_issue

        our_issue_id = str(resolve_issue(ticket)[0])
        order_id = extracted.get("order_id")

        def _d10(v):
            d = "".join(c for c in str(v or "") if c.isdigit())
            return d[-10:] if len(d) >= 10 else ""

        our_phone = _d10(phone)

        def _same_order(t):
            return bool(order_id) and str(t.get("shopifyOrderNo") or "") == str(order_id)

        def _same_issue(t):
            return str(t.get("issueId") or "") == our_issue_id

        def _same_phone(t):
            # VERIFIED CUSTOMER MATCH = the order owner's PHONE (resolved from Shopify), NOT the
            # email SENDER. One Gmail can submit complaints for DIFFERENT verified customers
            # (different order phones), which must NEVER merge into each other's ticket.
            cand = _d10(t.get("phone") or t.get("mobile") or t.get("customerPhone")
                        or t.get("customer_phone") or "")
            return bool(our_phone) and bool(cand) and cand == our_phone

        # Same issue AND the SAME verified customer -- same order, else same verified phone.
        chosen = (next((t for t in tickets if _same_order(t) and _same_issue(t)), None)
                  or next((t for t in tickets if _same_phone(t) and _same_issue(t)), None))

        # Debug: exactly which customer/order the phone lookup resolved to.
        logger.info("CUSTOMER-LOOKUP sender_email=%s phone=%s matched_customer=%s "
                    "matched_email=%s matched_order=%s lookup_source=%s",
                    ticket.customer_email, phone,
                    (chosen or {}).get("name"), (chosen or {}).get("email"),
                    (chosen or {}).get("shopifyOrderNo"),
                    "care_panel_open_tickets" if chosen else "none")

        if chosen is None:
            # Open tickets exist, but none for THIS verified customer + issue -> new ticket.
            logger.info("Care Panel %s: open tickets exist but none match verified phone=%s "
                        "order=%s issue_id=%s -> creating a new ticket.",
                        ticket.ticket_id, our_phone or "(none)", order_id or "(none)",
                        our_issue_id)
            AuditLogEntry.objects.create(
                ticket=ticket, actor="system", event="care_panel_no_matching_issue",
                detail={"our_issue_id": our_issue_id, "our_phone": our_phone,
                        "open": [{"phone": t.get("phone"), "issueId": t.get("issueId")}
                                 for t in tickets]},
            )
            return None

        # Real fields from the open-tickets response (verified live).
        cid = chosen.get("id") or chosen.get("ticket_id") or chosen.get("_id")
        tracking_url = chosen.get("url") or ""
        # ticketNumber comes prefixed with '#', e.g. '#2502110128' -> store without it
        # (the email template adds the '#').
        ticket_number = str(chosen.get("ticketNumber") or chosen.get("ticket_number") or "").lstrip("#")

        ticket.extracted = {**extracted, "care_panel_ticket_id": str(cid) if cid else "",
                            "care_panel_open_tickets": len(tickets)}
        fields = ["extracted", "updated_at"]
        if tracking_url:
            ticket.tracking_url = tracking_url
            fields.append("tracking_url")
        if ticket_number:
            ticket.ticket_number = ticket_number
            fields.append("ticket_number")
        ticket.save(update_fields=fields)
        AuditLogEntry.objects.create(
            ticket=ticket, actor="system", event="care_panel_ticket_matched",
            detail={"care_panel_ticket_id": str(cid), "ticket_number": ticket_number,
                    "tracking_url": tracking_url, "count": len(tickets)},
        )
        return cid

    # No open ticket in the Care Panel -> this is a new ticket.
    AuditLogEntry.objects.create(
        ticket=ticket, actor="system", event="care_panel_no_open_ticket",
        detail={"action": resp.get("action"), "reply": resp.get("reply", "")[:200]},
    )
    return None
